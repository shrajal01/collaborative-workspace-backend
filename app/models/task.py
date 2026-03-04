from sqlalchemy import Column, Integer, String, ForeignKey
from app.db import Base

class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)

    title = Column(String, nullable=False)
    description = Column(String)

    status = Column(String, default="todo")

    workspace_id = Column(Integer, ForeignKey("workspaces.id"))
    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)