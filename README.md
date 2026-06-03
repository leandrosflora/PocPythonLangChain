# PocPythonLangChain

PoC em Python de um **agente conversacional para WhatsApp** focado em jornada de **empréstimo consignado**.

O projeto funciona como uma camada de agente/orquestração que recebe mensagens do usuário, controla o estado da conversa e chama tools externas por meio de um servidor MCP local.

> Status: **POC / desenvolvimento local**. Não está pronto para produção.

---

## Objetivo

Este repositório demonstra uma abordagem simples para construir um agente conversacional orientado a jornada.

O agente conduz o cliente por uma simulação e contratação de consignado, usando uma máquina de estados e chamadas a tools MCP.

Fluxo principal:

1. Receber mensagem do WhatsApp.
2. Identificar se a intenção está dentro do escopo de consignado.
3. Buscar cliente pelo telefone.
4. Validar CPF quando necessário.
5. Solicitar valor da simulação.
6. Consultar elegibilidade.
7. Buscar ofertas.
8. Exibir opções ao usuário.
9. Receber escolha da oferta.
10. Criar contrato.
11. Gerar link de formalização.

---

## Arquitetura geral

```text
WhatsApp / Canal Conversacional
        |
        v
+-----------------------------+
| Agent API - FastAPI         |
| POST /agent/whatsapp        |
+-------------+---------------+
              |
              v
+-----------------------------+
| Agent Core                  |
| - State machine             |
| - Intent gate               |
| - Text parsing              |
| - Jornada por conversation  |
+-------------+---------------+
              |
              v
+-----------------------------+
| MCP Client                  |
| Chamada HTTP para /call     |
+-------------+---------------+
              |
              v
+-----------------------------+
| MCP Server / Tools          |
| lookup_customer_by_phone    |
| resolve_identity_by_cpf     |
| check_eligibility           |
| get_loan_offers             |
| create_contract             |
| get_formalization_link      |
+-----------------------------+
```

---

## Natureza do agente

Este projeto é um **agente conversacional baseado em fluxo/estado**, não um agente LLM autônomo completo.

Ele usa:

- parsing simples de texto;
- detecção básica de intenção por palavras-chave;
- extração de CPF e valor por regex;
- máquina de estados;
- chamadas a tools via MCP.

A dependência conceitual com LangChain/agents aparece no propósito da PoC, mas o núcleo atual do agente é majoritariamente determinístico.

---

## Stack

Principais tecnologias usadas ou esperadas pelo código:

- Python
- FastAPI
- Pydantic
- HTTPX
- Uvicorn
- boto3, presente nas dependências

> Observação: o código usa `fastapi`, `pydantic` e `httpx`, mas o `requirements.txt` atual pode não listar todas essas dependências. Antes de rodar, valide e ajuste as dependências.

---

## Estrutura do projeto

```text
.
├── agent.py
├── agent_api.py
├── run_agent.py
├── requirements.txt
└── env/
```

### Responsabilidades principais

| Arquivo | Responsabilidade |
|---|---|
| `agent.py` | Implementa o agente, máquina de estados, parsing de texto e chamadas ao MCP. |
| `agent_api.py` | Expõe API FastAPI para invocar o agente via HTTP. |
| `run_agent.py` | Script utilitário para execução local/manual do agente. |
| `requirements.txt` | Lista de dependências Python. |
| `env/` | Ambiente virtual local incluído no repositório. Idealmente deve ser ignorado via `.gitignore`. |

---

## Máquina de estados

O agente controla a jornada usando os seguintes estados:

| Estado | Significado |
|---|---|
| `START` | Início da conversa ou estado inicial. |
| `ASK_CPF` | Agente precisa validar CPF do cliente. |
| `ASK_AMOUNT` | Agente precisa do valor desejado para simulação. |
| `SHOW_OFFERS` | Estado conceitual para apresentação de ofertas. |
| `AWAIT_OFFER_CONFIRMATION` | Agente aguarda escolha da oferta pelo usuário. |
| `DONE` | Jornada finalizada. |
| `OUT_OF_SCOPE` | Mensagem fora do escopo de consignado. |

---

## Endpoint

### `POST /agent/whatsapp`

Endpoint principal para invocar o agente.

Request:

```json
{
  "phone": "+5511999999999",
  "text": "quero simular 5000",
  "conversationId": "conv-123"
}
```

Campos:

| Campo | Obrigatório | Descrição |
|---|---:|---|
| `phone` | Sim | Telefone do cliente. |
| `text` | Sim | Texto recebido do usuário. |
| `conversationId` | Não | Identificador da conversa. Se ausente, usa o telefone como chave. |

Resposta típica:

```json
{
  "whatsappReply": "Para sua segurança, me envie seu CPF (somente números).",
  "stage": "ASK_CPF"
}
```

Resposta com ofertas:

```json
{
  "whatsappReply": "Encontrei estas opções (responda com 1, 2 ou 3):\n1) Parcela: 250.0 | Prazo: 24 meses | Taxa: 0.018",
  "offers": [
    {
      "id": "offer-123",
      "installment": 250.0,
      "termMonths": 24,
      "rateMonthly": 0.018
    }
  ],
  "stage": "AWAIT_OFFER_CONFIRMATION"
}
```

Resposta com formalização:

```json
{
  "whatsappReply": "Perfeito. Para continuar a formalização, acesse: https://exemplo.com/formalizacao/contract-123\nValidade: 2026-01-01T12:00:00Z",
  "contractId": "contract-123",
  "formalizationUrl": "https://exemplo.com/formalizacao/contract-123",
  "expiresAt": "2026-01-01T12:00:00Z",
  "stage": "DONE"
}
```

---

## Integração com MCP

O agente chama o MCP Server por HTTP.

Configuração padrão:

```python
MCP_BASE_URL = os.getenv("MCP_BASE_URL", "http://localhost:8001")
MCP_TOKEN = os.getenv("MCP_TOKEN", "")
```

Variáveis de ambiente:

```bash
MCP_BASE_URL=http://localhost:8001
MCP_TOKEN=local-dev-token
```

O client envia chamadas para:

```text
POST {MCP_BASE_URL}/call
```

Envelope enviado ao MCP:

```json
{
  "tool": "lookup_customer_by_phone",
  "input": {
    "phone": "+5511999999999"
  },
  "context": {
    "correlationId": "uuid-da-jornada",
    "channel": "whatsapp",
    "subject": "consignado-agent-whatsapp"
  }
}
```

---

## Tools MCP usadas

O agente depende das seguintes tools no MCP Server:

| Tool | Papel |
|---|---|
| `lookup_customer_by_phone` | Busca cliente pelo telefone. |
| `get_identity_context` | Recupera contexto de identidade da jornada. |
| `resolve_identity_by_cpf` | Valida CPF e resolve identidade. |
| `check_eligibility` | Consulta elegibilidade para consignado. |
| `get_loan_offers` | Busca ofertas de empréstimo. |
| `create_contract` | Cria contrato pendente. |
| `get_formalization_link` | Gera link de formalização. |

---

## Fluxo conversacional

### 1. Usuário inicia conversa

Exemplo:

```text
quero simular 5000
```

O agente:

- valida se a intenção é de consignado;
- busca o cliente pelo telefone;
- verifica se há identidade resolvida;
- pede CPF se necessário.

---

### 2. Usuário envia CPF

Exemplo:

```text
12345678901
```

O agente:

- extrai os 11 dígitos;
- chama `resolve_identity_by_cpf`;
- salva `customerRef` e `cpfToken` na jornada;
- pede o valor da simulação, se ainda não tiver.

---

### 3. Usuário informa valor

Exemplo:

```text
5000
```

O agente:

- extrai o valor;
- consulta elegibilidade;
- busca ofertas;
- apresenta até 3 opções.

---

### 4. Usuário escolhe oferta

Exemplo:

```text
1
```

O agente:

- identifica a oferta escolhida;
- cria contrato;
- gera link de formalização;
- encerra a jornada.

---

## Execução local

### 1. Criar ambiente virtual

```bash
python -m venv env
```

### 2. Ativar ambiente virtual

Windows PowerShell:

```bash
.\env\Scripts\Activate.ps1
```

Windows CMD:

```bash
env\Scripts\activate.bat
```

Linux/macOS:

```bash
source env/bin/activate
```

### 3. Instalar dependências

```bash
pip install -r requirements.txt
```

Se faltarem dependências em runtime, instalar também:

```bash
pip install fastapi uvicorn httpx pydantic
```

### 4. Configurar MCP

```bash
export MCP_BASE_URL=http://localhost:8001
export MCP_TOKEN=local-dev-token
```

No Windows PowerShell:

```powershell
$env:MCP_BASE_URL="http://localhost:8001"
$env:MCP_TOKEN="local-dev-token"
```

### 5. Subir a API do agente

```bash
uvicorn agent_api:app --reload --port 8000
```

A API ficará disponível em:

```text
http://127.0.0.1:8000
```

Swagger/OpenAPI:

```text
http://127.0.0.1:8000/docs
```

---

## Exemplo com cURL

```bash
curl -X POST http://127.0.0.1:8000/agent/whatsapp \
  -H "Content-Type: application/json" \
  -d '{
    "phone": "+5511999999999",
    "text": "quero simular 5000",
    "conversationId": "conv-001"
  }'
```

Exemplo enviando CPF:

```bash
curl -X POST http://127.0.0.1:8000/agent/whatsapp \
  -H "Content-Type: application/json" \
  -d '{
    "phone": "+5511999999999",
    "text": "12345678901",
    "conversationId": "conv-001"
  }'
```

Exemplo escolhendo oferta:

```bash
curl -X POST http://127.0.0.1:8000/agent/whatsapp \
  -H "Content-Type: application/json" \
  -d '{
    "phone": "+5511999999999",
    "text": "1",
    "conversationId": "conv-001"
  }'
```

---

## Parsing de texto

O agente possui extrações simples:

### CPF

Extrai somente se encontrar exatamente 11 dígitos.

Exemplos aceitos:

```text
12345678901
123.456.789-01
```

### Valor

Aceita formatos como:

```text
5000
5.000
5,000
5 mil
```

### Intenção de consignado

Considera termos como:

```text
consignado
empréstimo
credito
crédito
parcela
simular
simulação
oferta
taxa
```

---

## Tratamento de erro

O agente captura erros do MCP e retorna mensagens simplificadas para o usuário final.

Exemplos:

| Situação | Resposta ao usuário |
|---|---|
| MCP indisponível no lookup | `Serviço instável agora. Tenta de novo em alguns minutos.` |
| Falha na identidade | `Não consegui validar sua identidade agora. Tente novamente mais tarde.` |
| Falha na simulação | `Não consegui simular agora. Tente novamente em instantes.` |
| Falha na formalização | `Deu ruim na formalização agora. Tente novamente em instantes.` |

---

## Limitações atuais

Este projeto ainda tem características claras de POC:

- Estado da conversa em memória (`JOURNEYS`).
- Sem persistência externa.
- Sem autenticação própria na API do agente.
- Sem integração real com WhatsApp/Blip/Zenvia/Twilio.
- Sem fila ou mecanismo assíncrono.
- Sem observabilidade estruturada.
- Sem testes automatizados documentados.
- Sem Dockerfile e sem pipeline de CI/CD.
- `env/` está versionado, o que não é recomendado.
- `requirements.txt` aparenta estar incompleto para rodar a API atual.
- Não há uso forte de LLM no fluxo atual; a decisão é majoritariamente determinística.

---

## Melhorias recomendadas

Antes de evoluir para produção ou demo executiva mais robusta:

- Remover `env/` do repositório e adicionar ao `.gitignore`.
- Corrigir `requirements.txt` com dependências reais.
- Adicionar Dockerfile.
- Adicionar testes unitários para parsing e state machine.
- Adicionar testes de contrato para o MCP Client.
- Persistir jornada em Redis, DynamoDB ou banco equivalente.
- Adicionar autenticação/autorização no endpoint do agente.
- Integrar com um canal real de WhatsApp.
- Adicionar logs estruturados com `conversationId` e `correlationId`.
- Adicionar métricas de erro, latência e conversão por etapa.
- Separar regras de negócio, infraestrutura e adaptadores.
- Avaliar uso real de LLM para NLU, fallback e respostas mais naturais.

---

## Relação com o MCP Server

Este repositório deve ser usado junto com o servidor MCP local, por exemplo o projeto `McpServerPython`.

Divisão de responsabilidades sugerida:

| Componente | Responsabilidade |
|---|---|
| `PocPythonLangChain` | Agente conversacional, jornada, estado e experiência do usuário. |
| `McpServerPython` | Exposição de tools de negócio e integração com APIs de domínio. |

---

## Veredito técnico

Este repo é útil como **PoC de agente conversacional para consignado**, principalmente para demonstrar como um agente pode orquestrar uma jornada usando tools MCP.

Ele não é, ainda, uma solução de agente corporativo completa. O valor está na clareza do fluxo, na separação entre agente e tools, e na possibilidade de evoluir para um canal conversacional real integrado a APIs bancárias.
