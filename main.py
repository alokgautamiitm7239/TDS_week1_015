
from fastapi import FastAPI
from pydantic import BaseModel
from ledger import answer_question

app = FastAPI(title="Acme Appliances Ledger Agent")


class QuestionRequest(BaseModel):
    question: str


@app.get("/")
def home():
    return {"status": "Ledger Agent is running"}


@app.post("/")
def ask_question(request: QuestionRequest):
    answer = answer_question(request.question)
    return {"answer": answer}
