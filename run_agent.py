import uvicorn

if __name__ == "__main__":
    uvicorn.run("agent_api:app", host="127.0.0.1", port=7000, reload=False)
