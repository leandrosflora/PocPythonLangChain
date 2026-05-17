from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import httpx


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
        resp = httpx.post(f"{self.base_url}/call", json=payload, headers=self.headers, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()

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
        return data.get("output", data)


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


def _parse_offer_selection(text: str, offers: List[Dict[str, Any]]) -> Optional[str]:
    t = (text or "").strip()
    if t not in {"1", "2", "3"}:
        return None
    idx = int(t) - 1
    if idx < 0 or idx >= len(offers):
        return None
    off = offers[idx]
    return off.get("id") or off.get("offerId") or off.get("OfferId")


def _is_consignado_intent(text: str) -> bool:
    t = (text or "").lower()
    keywords = ["consign", "empréstimo", "emprestimo", "crédito", "credito", "parcela", "simular", "simulação", "simulacao", "oferta", "taxa"]
    if any(k in t for k in keywords):
        return True
    if _extract_amount(text) is not None:
        return True
    if _is_offer_choice(text):
        return True
    if _extract_cpf(text) is not None:
        return True
    return False


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


# =========================
# MCP wrappers (workflow steps)
# =========================
def _lookup(j: Journey) -> Dict[str, Any]:
    out = mcp.call("lookup_customer_by_phone", {"phone": j.phone}, _ctx(j))
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
# Main entrypoint
# =========================
def handle_whatsapp_message(phone: str, text: str, conversationId: Optional[str] = None) -> Dict[str, Any]:
    conversation_id = conversationId or phone
    j = get_journey(conversation_id, channel="whatsapp")
    j.phone = phone

    # Gate fora do escopo (não bloqueia CPF / escolha)
    if j.stage != Stage.AWAIT_OFFER_CONFIRMATION and not _is_consignado_intent(text):
        return _out_of_scope_reply()

    # 1) Se aguardando confirmação: contrata
    if j.stage == Stage.AWAIT_OFFER_CONFIRMATION and j.last_offers:
        offer_id = _parse_offer_selection(text, j.last_offers)
        if not offer_id:
            return {"whatsappReply": "Escolha inválida. Responda com 1, 2 ou 3.", "stage": Stage.AWAIT_OFFER_CONFIRMATION.value}

        try:
            ident = _identity_context(j)
            if not ident.get("hasIdentity"):
                j.stage = Stage.ASK_CPF
                return {"whatsappReply": "Perdi seu contexto. Me envie seu CPF novamente (somente números).", "stage": j.stage.value}

            contract = _create_contract(j, offer_id)
            contract_id = contract.get("contractId")
            if not contract_id:
                return {"whatsappReply": "Falhou ao criar o contrato. Tente novamente em instantes.", "stage": j.stage.value}

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

        except MCPError:
            # sem detalhes sensíveis pro usuário
            return {"whatsappReply": "Deu ruim na formalização agora. Tente novamente em instantes.", "stage": j.stage.value}

    # 2) Sempre faz lookup no whatsapp
    try:
        lookup = _lookup(j)
        if not lookup.get("found"):
            j.stage = Stage.START
            return {"whatsappReply": "Não encontrei seu cadastro. Confira seu número ou procure atendimento humano.", "stage": j.stage.value}

    except MCPError:
        return {"whatsappReply": "Serviço instável agora. Tenta de novo em alguns minutos.", "stage": j.stage.value}

    # 3) Identidade: se não tem, pede CPF
    try:
        ident = _identity_context(j)
        if not ident.get("hasIdentity"):
            cpf = _extract_cpf(text)
            if not cpf:
                j.stage = Stage.ASK_CPF
                return {"whatsappReply": "Para sua segurança, me envie seu CPF (somente números).", "stage": j.stage.value}

            res = _resolve_cpf(j, cpf)
            if not res.get("resolved"):
                j.stage = Stage.ASK_CPF
                return {"whatsappReply": "CPF não conferiu. Tente novamente (somente números).", "stage": j.stage.value}

    except MCPError:
        return {"whatsappReply": "Não consegui validar sua identidade agora. Tente novamente mais tarde.", "stage": j.stage.value}

    # 4) Valor
    amount = _extract_amount(text)
    if amount is None:
        j.stage = Stage.ASK_AMOUNT
        return {"whatsappReply": "Qual valor você quer simular? Ex: 5000", "stage": j.stage.value}
    j.last_requested_amount = amount

    # 5) Elegibilidade + ofertas
    try:
        elig = _check_eligibility(j)
        if not elig.get("eligible", True):
            j.stage = Stage.DONE
            return {"whatsappReply": "No momento você não está elegível para consignado.", "stage": j.stage.value}

        offers = _get_offers(j, amount)
        if not offers:
            j.stage = Stage.DONE
            return {"whatsappReply": "Não encontrei ofertas agora. Tente um valor diferente ou mais tarde.", "stage": j.stage.value}

        j.stage = Stage.AWAIT_OFFER_CONFIRMATION
        reply = _format_offers_for_whatsapp(offers)
        return {"whatsappReply": reply, "offers": offers[:3], "stage": j.stage.value}

    except MCPError:
        return {"whatsappReply": "Não consegui simular agora. Tente novamente em instantes.", "stage": j.stage.value}


if __name__ == "__main__":
    print(handle_whatsapp_message("+5511999999999", "quero 5000"))
    print(handle_whatsapp_message("+5511999999999", "123.456.789-09"))
    print(handle_whatsapp_message("+5511999999999", "5000"))
    print(handle_whatsapp_message("+5511999999999", "1"))
