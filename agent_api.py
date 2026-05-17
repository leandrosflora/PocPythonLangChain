import traceback
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

from agent import handle_whatsapp_message

app = FastAPI(title="Consignado Agent Service")

class WhatsAppRequest(BaseModel):
    phone: str
    text: str
    conversationId: Optional[str] = None

@app.post("/agent/whatsapp")
def invoke_agent(payload: WhatsAppRequest):
    try:
        result = handle_whatsapp_message(phone=payload.phone, text=payload.text)
        return result
    except Exception as e:
        tb = traceback.format_exc()
        print("=== AGENT ERROR ===")
        print(tb)
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "trace": tb}
        )
