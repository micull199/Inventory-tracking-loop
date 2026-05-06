from fastapi import FastAPI

app = FastAPI(title="UC Inventory")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
