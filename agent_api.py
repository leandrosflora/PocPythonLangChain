import logging
import traceback
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agent import handle_whatsapp_message


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Consignado Agent Service")


class WhatsAppRequest(BaseModel):
    phone: str
    text: str
    conversationId: Optional[str] = None


@app.post("/agent/whatsapp")
def invoke_agent(payload: WhatsAppRequest):
    try:
        return handle_whatsapp_message(
            phone=payload.phone,
            text=payload.text,
            conversationId=payload.conversationId,
        )
    except Exception as e:
        tb = traceback.format_exc()
        logger.exception("agent_api_unhandled_error")
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "trace": tb},
        )
