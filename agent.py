from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


# =========================
# User-facing messages
# =========================
MSG_ASK_CPF = "Para sua segurança, me envie seu CPF (somente números)."
MSG_ASK_AMOUNT = "Qual valor você quer simular? Ex: 5000"
MSG_INVALID_OFFER = "Escolha inválida. Responda com 1, 2 ou 3."
MSG_CUSTOMER_NOT_FOUND = "Não encontrei seu cadastro. Confira seu número ou procure atendimento humano."
MSG_LOOKUP_UNSTABLE = "Serviço instável agora. Tenta de novo em alguns minutos."
MSG_IDENTITY_UNAVAILABLE = "Não consegui validar sua identidade agora. Tente novamente mais tarde."
MSG_SIMULATION_UNAVAILABLE = "Não consegui simular agora. Tente novamente em instantes."
MSG_FORMALIZATION_UNAVAILABLE = "Deu ruim na formalização agora. Tente novamente em instantes."
MSG_CONTRACT_CREATION_FAILED = "Falhou ao criar o contrato. Tente novamente em instantes."
MSG_CONTEXT_LOST = "Perdi seu contexto. Me envie seu CPF novamente (somente números)."
MSG_NOT_ELIGIBLE = "No momento você não está elegível para consignado."
MSG_NO_OFFERS = "Não encontrei ofertas agora. Tente um valor diferente ou mais tarde."


# =========================
# MCP Client (Harnessed)
# =========================
class MCPError(Exception):
    def __init__(self, code: str, message: str, retriable: bool = False, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retriable = retriable
        self.details = details or {}


class MCPClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 25.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def call(self, tool_name: str, tool_input: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        payload = {"tool": tool_name, "input": tool_input, "context": context}

        try:
            resp = httpx.post(
                f"{self.base_url}/call",
                json=payload,
                headers=self.headers,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.TimeoutException as e:
            raise MCPError(
                "MCP_TIMEOUT",
                "Timeout ao chamar MCP",
                retriable=True,
                details={"tool": tool_name},
            ) from e
        except httpx.HTTPStatusError as e:
            raise MCPError(
                "MCP_HTTP_ERROR",
                f"Erro HTTP ao chamar MCP: {e.response.status_code}",
                retriable=True,
                details={"tool": tool_name, "status_code": e.response.status_code},
            ) from e
        except Exception as e:
            raise MCPError(
                "MCP_UNAVAILABLE",
                "MCP indisponível ou resposta inválida",
                retriable=True,
                details={"tool": tool_name},
            ) from e

        # Harness envelope: {ok, output?, error?, trace}
        if isinstance(data, dict) and data.get("ok") is True:
            return data.get("output") or {}

        if isinstance(data, dict) and data.get("ok") is False:
            err = data.get("error") or {}
            raise MCPError(
                code=err.get("code", "MCP_ERROR"),
                message=err.get("message", "Erro no MCP"),
                retriable=bool(err.get("retriable", False)),
                details=err.get("details") or {},
            )

        # fallback (compat)
        if isinstance(data, dict):
            return data.get("output", data)
        return {"raw": data}


MCP_BASE_URL = os.getenv("MCP_BASE_URL", "http://localhost:8001")
MCP_TOKEN = os.getenv("MCP_TOKEN", "")
mcp = MCPClient(MCP_BASE_URL, MCP_TOKEN)


# =========================
# Workflow / State machine
# =========================
class Stage(str, Enum):
    START = "START"
    ASK_CPF = "ASK_CPF"
    ASK_AMOUNT = "ASK_AMOUNT"
    SHOW_OFFERS = "SHOW_OFFERS"
    AWAIT_OFFER_CONFIRMATION = "AWAIT_OFFER_CONFIRMATION"
    DONE = "DONE"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


@dataclass
class Journey:
    correlation_id: str
    channel: str
    subject: str

    phone: Optional[str] = None
    stage: Stage = Stage.START

    lookup_done: bool = False
    customerRef: Optional[str] = None
    has_identity: bool = False
    cpfToken: Optional[str] = None

    eligibilityId: Optional[str] = None

    last_requested_amount: Optional[float] = None
    last_offers: Optional[List[Dict[str, Any]]] = None


JOURNEYS: Dict[str, Journey] = {}  # DEV only


def _ctx(j: Journey) -> Dict[str, Any]:
    return {"correlationId": j.correlation_id, "channel": j.channel, "subject": j.subject}


def get_journey(conversation_id: str, channel: str) -> Journey:
    j = JOURNEYS.get(conversation_id)
    if not j:
        j = Journey(correlation_id=str(uuid.uuid4()), channel=channel, subject="consignado-agent-whatsapp")
        JOURNEYS[conversation_id] = j
    return j


# =========================
# Text parsing
# =========================
def _digits(s: str) -> str:
    return "".join([c for c in (s or "") if c.isdigit()])


def _extract_cpf(text: str) -> Optional[str]:
    d = _digits(text)
    if len(d) == 11:
        return d
    return None


def _extract_amount(text: str) -> Optional[float]:
    # aceita: 5000, 5.000, 5,000, 5 mil
    t = (text or "").lower()

    m = re.search(r"(\d+[\d\.,]*)\s*(mil)?", t)
    if not m:
        return None

    raw = m.group(1)
    is_mil = bool(m.group(2))

    raw = raw.replace(".", "").replace(",", ".")
    try:
        v = float(raw)
    except Exception:
        return None

    if is_mil and v < 1000:
        v = v * 1000.0

    if v <= 0:
        return None
    return float(v)


def _is_offer_choice(text: str) -> bool:
    return (text or "").strip() in {"1", "2", "3"}


@dataclass(frozen=True)
class ParsedMessage:
    text: str
    cpf: Optional[str]
    amount: Optional[float]
    is_offer_choice: bool
    is_consignado_intent: bool


def parse_message(text: str) -> ParsedMessage:
    cpf = _extract_cpf(text)
    amount = _extract_amount(text)
    is_offer_choice = _is_offer_choice(text)

    t = (text or "").lower()
    keywords = [
        "consign",
        "empréstimo",
        "emprestimo",
        "crédito",
        "credito",
        "parcela",
        "simular",
        "simulação",
        "simulacao",
        "oferta",
        "taxa",
    ]

    is_consignado_intent = (
        any(k in t for k in keywords)
        or amount is not None
        or is_offer_choice
        or cpf is not None
    )

    return ParsedMessage(
        text=text,
        cpf=cpf,
        amount=amount,
        is_offer_choice=is_offer_choice,
        is_consignado_intent=is_consignado_intent,
    )


def _parse_offer_selection(text: str, offers: List[Dict[str, Any]]) -> Optional[str]:
    t = (text or "").strip()
    if t not in {"1", "2", "3"}:
        return None
    idx = int(t) - 1
    if idx < 0 or idx >= len(offers):
        return None
    off = offers[idx]
    return off.get("id") or off.get("offerId") or off.get("OfferId")


def _format_offers_for_whatsapp(offers: List[Dict[str, Any]]) -> str:
    lines = ["Encontrei estas opções (responda com 1, 2 ou 3):"]
    for i, o in enumerate(offers[:3], start=1):
        inst = o.get("installment") or o.get("Installment")
        term = o.get("termMonths") or o.get("TermMonths") or o.get("term")
        rate = o.get("rateMonthly") or o.get("RateMonthly") or o.get("rate")
        lines.append(f"{i}) Parcela: {inst} | Prazo: {term} meses | Taxa: {rate}")
    return "\n".join(lines)


def _out_of_scope_reply() -> Dict[str, Any]:
    return {
        "whatsappReply": (
            "Eu só ajudo com *empréstimo consignado* (simulação e contratação). "
            "Me diga o *valor* que quer simular."
        ),
        "stage": Stage.OUT_OF_SCOPE.value,
    }


def _reply(message: str, stage: Stage) -> Dict[str, Any]:
    return {"whatsappReply": message, "stage": stage.value}


def _log_mcp_error(step: str, err: MCPError, journey: Journey) -> None:
    logger.warning(
        "mcp_error step=%s code=%s retriable=%s correlation_id=%s stage=%s details=%s",
        step,
        err.code,
        err.retriable,
        journey.correlation_id,
        journey.stage.value,
        err.details,
    )


# =========================
# MCP wrappers (workflow steps)
# =========================
def _lookup(j: Journey) -> Dict[str, Any]:
    out = mcp.call("lookup_customer_by_phone", {"phone": j.phone}, _ctx(j))
    j.lookup_done = True
    if out.get("customerRef"):
        j.customerRef = out.get("customerRef")
    return out


def _identity_context(j: Journey) -> Dict[str, Any]:
    out = mcp.call("get_identity_context", {}, _ctx(j))
    j.has_identity = bool(out.get("hasIdentity"))
    j.customerRef = out.get("customerRef") or j.customerRef
    j.cpfToken = out.get("cpfToken") or j.cpfToken
    return out


def _resolve_cpf(j: Journey, cpf: str) -> Dict[str, Any]:
    out = mcp.call("resolve_identity_by_cpf", {"phone": j.phone, "cpf": cpf}, _ctx(j))
    if out.get("resolved"):
        j.has_identity = True
        j.customerRef = out.get("customerRef") or j.customerRef
        j.cpfToken = out.get("cpfToken") or j.cpfToken
    return out


def _check_eligibility(j: Journey) -> Dict[str, Any]:
    out = mcp.call("check_eligibility", {"customerRef": j.customerRef, "channel": j.channel}, _ctx(j))
    j.eligibilityId = out.get("eligibilityId") or j.eligibilityId
    return out


def _get_offers(j: Journey, amount: float) -> List[Dict[str, Any]]:
    out = mcp.call("get_loan_offers", {"requestedAmount": float(amount)}, _ctx(j))
    offers = out.get("offers") or []
    j.last_offers = offers
    return offers


def _create_contract(j: Journey, offer_id: str) -> Dict[str, Any]:
    idem = str(uuid.uuid4())
    return mcp.call("create_contract", {"offerId": offer_id, "idempotencyKey": idem}, _ctx(j))


def _get_formalization_link(j: Journey, contract_id: str) -> Dict[str, Any]:
    return mcp.call("get_formalization_link", {"contractId": contract_id}, _ctx(j))


# =========================
# Flow helpers
# =========================
def _ensure_customer_lookup(j: Journey) -> Optional[Dict[str, Any]]:
    if j.lookup_done or j.customerRef:
        return None

    try:
        lookup = _lookup(j)
        if not lookup.get("found"):
            j.stage = Stage.START
            return _reply(MSG_CUSTOMER_NOT_FOUND, j.stage)
        return None
    except MCPError as err:
        _log_mcp_error("lookup", err, j)
        return _reply(MSG_LOOKUP_UNSTABLE, j.stage)


def _ensure_identity(j: Journey, parsed: ParsedMessage) -> Optional[Dict[str, Any]]:
    try:
        ident = _identity_context(j)
        if ident.get("hasIdentity"):
            return None

        if not parsed.cpf:
            j.stage = Stage.ASK_CPF
            return _reply(MSG_ASK_CPF, j.stage)

        res = _resolve_cpf(j, parsed.cpf)
        if not res.get("resolved"):
            j.stage = Stage.ASK_CPF
            return _reply("CPF não conferiu. Tente novamente (somente números).", j.stage)

        return None
    except MCPError as err:
        _log_mcp_error("identity", err, j)
        return _reply(MSG_IDENTITY_UNAVAILABLE, j.stage)


def _handle_offer_confirmation(j: Journey, parsed: ParsedMessage) -> Dict[str, Any]:
    offers = j.last_offers or []
    offer_id = _parse_offer_selection(parsed.text, offers)
    if not offer_id:
        return _reply(MSG_INVALID_OFFER, Stage.AWAIT_OFFER_CONFIRMATION)

    try:
        ident = _identity_context(j)
        if not ident.get("hasIdentity"):
            j.stage = Stage.ASK_CPF
            return _reply(MSG_CONTEXT_LOST, j.stage)

        contract = _create_contract(j, offer_id)
        contract_id = contract.get("contractId")
        if not contract_id:
            return _reply(MSG_CONTRACT_CREATION_FAILED, j.stage)

        link = _get_formalization_link(j, contract_id)
        url = link.get("formalizationUrl")
        expires = link.get("expiresAt")

        j.stage = Stage.DONE
        j.last_offers = None
        return {
            "whatsappReply": f"Perfeito. Para continuar a formalização, acesse: {url}\nValidade: {expires}",
            "contractId": contract_id,
            "formalizationUrl": url,
            "expiresAt": expires,
            "stage": j.stage.value,
        }
    except MCPError as err:
        _log_mcp_error("formalization", err, j)
        return _reply(MSG_FORMALIZATION_UNAVAILABLE, j.stage)


def _handle_simulation_flow(j: Journey, parsed: ParsedMessage) -> Dict[str, Any]:
    lookup_reply = _ensure_customer_lookup(j)
    if lookup_reply:
        return lookup_reply

    identity_reply = _ensure_identity(j, parsed)
    if identity_reply:
        return identity_reply

    if parsed.amount is None:
        j.stage = Stage.ASK_AMOUNT
        return _reply(MSG_ASK_AMOUNT, j.stage)

    j.last_requested_amount = parsed.amount

    try:
        elig = _check_eligibility(j)
        if not elig.get("eligible", True):
            j.stage = Stage.DONE
            return _reply(MSG_NOT_ELIGIBLE, j.stage)

        offers = _get_offers(j, parsed.amount)
        if not offers:
            j.stage = Stage.DONE
            return _reply(MSG_NO_OFFERS, j.stage)

        j.stage = Stage.AWAIT_OFFER_CONFIRMATION
        reply = _format_offers_for_whatsapp(offers)
        return {"whatsappReply": reply, "offers": offers[:3], "stage": j.stage.value}
    except MCPError as err:
        _log_mcp_error("simulation", err, j)
        return _reply(MSG_SIMULATION_UNAVAILABLE, j.stage)


# =========================
# Main entrypoint
# =========================
def handle_whatsapp_message(phone: str, text: str, conversationId: Optional[str] = None) -> Dict[str, Any]:
    conversation_id = conversationId or phone
    j = get_journey(conversation_id, channel="whatsapp")
    j.phone = phone

    parsed = parse_message(text)

    # Gate fora do escopo. Não bloqueia a etapa de escolha de oferta.
    if j.stage != Stage.AWAIT_OFFER_CONFIRMATION and not parsed.is_consignado_intent:
        j.stage = Stage.OUT_OF_SCOPE
        return _out_of_scope_reply()

    if j.stage == Stage.AWAIT_OFFER_CONFIRMATION and j.last_offers:
        return _handle_offer_confirmation(j, parsed)

    return _handle_simulation_flow(j, parsed)


if __name__ == "__main__":
    print(handle_whatsapp_message("+5511999999999", "quero 5000"))
    print(handle_whatsapp_message("+5511999999999", "123.456.789-09"))
    print(handle_whatsapp_message("+5511999999999", "5000"))
    print(handle_whatsapp_message("+5511999999999", "1"))
