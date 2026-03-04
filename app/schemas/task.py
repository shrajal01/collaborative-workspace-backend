from pydantic import BaseModel, Field
from typing import Optional
from enum import Enum
from datetime import datetime

class TaskCreate(BaseModel):
    title: str = Field(..., min_length=3, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    assigned_to: int = Field(..., gt=0)

class TaskStatus(str, Enum):
    todo = "todo"
    in_progress = "in_progress"
    done = "done"

class TaskResponse(BaseModel):
    id: int
    title: str
    description: Optional[str]
    status: str
    workspace_id: int
    assigned_to: int

    model_config = {
    "from_attributes": True
}