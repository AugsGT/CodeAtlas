from pydantic import BaseModel


class IngestRequest(BaseModel):
    repo_root: str


class ResolvedTarget(BaseModel):
    kind: str | None = None
    id: str | None = None


class HistoryTurn(BaseModel):
    question: str
    answer: str
    resolved_target: ResolvedTarget | None = None


class AskRequest(BaseModel):
    question: str
    history: list[HistoryTurn] = []


class ValidateFixRequest(BaseModel):
    repo_root: str
    entity_id: str
    improved_code: str


class ApplyFixRequest(BaseModel):
    repo_root: str
    entity_id: str
    improved_code: str
