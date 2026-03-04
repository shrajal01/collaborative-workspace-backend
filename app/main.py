from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, WebSocket, Request
from sqlalchemy.orm import Session
from app import db
from app.db import engine, Base, get_db

# ye import karna compulsory hai
from app.models.user import User
from app.models.workspace import Workspace
from app.models.membership import WorkspaceMember

from app.schemas import task
from app.schemas.user import UserCreate, UserLogin
from app.core.security import hash_password, verify_password

from app.core.auth import create_access_token, get_current_user
from sqlalchemy import select

from app.schemas.workspace import WorkspaceCreate
from app.core.auth import get_current_user

from app.models.task import Task
from app.schemas.task import TaskCreate, TaskStatus, TaskResponse

from app.core.cache import redis_client
import json
import asyncio

from app.models.activity import ActivityLog

from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi import _rate_limit_exceeded_handler

app = FastAPI()

limiter = Limiter(key_func=get_remote_address)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

Base.metadata.create_all(bind=engine)


class ConnectionManager:

    def __init__(self):
        self.active_connections = {}

    async def connect(self, workspace_id: int, websocket: WebSocket):
        await websocket.accept()

        if workspace_id not in self.active_connections:
            self.active_connections[workspace_id] = []

        self.active_connections[workspace_id].append(websocket)

    def disconnect(self, workspace_id: int, websocket: WebSocket):
        self.active_connections[workspace_id].remove(websocket)

    async def broadcast(self, workspace_id: int, message: str):
        if workspace_id in self.active_connections:
            for connection in self.active_connections[workspace_id]:
                await connection.send_text(message)


manager = ConnectionManager()


def is_admin(user_id: int, workspace_id: int, db: Session):

    member = db.query(WorkspaceMember).filter(
        WorkspaceMember.user_id == user_id,
        WorkspaceMember.workspace_id == workspace_id
    ).first()

    if not member or member.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    
def send_invite_email(email: str):
    print(f"Sending email to {email}")

def invalidate_task_cache(workspace_id: int):
    try:
        redis_client.delete(f"workspace:{workspace_id}:tasks:0:5")
    except Exception:
        # Redis down? Ignore.
        pass



@app.get("/")
def home():
    return {"msg": "running"}


@app.post("/register")
@limiter.limit("3/minute")
def register(request: Request, data: UserCreate, db: Session = Depends(get_db)):

    existing_user = db.query(User).filter(User.email == data.email).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")

    hashed = hash_password(data.password)

    user = User(email=data.email, password_hash=hashed)

    try:
            db.add(user)
            db.commit()
            db.refresh(user)
    except:
            db.rollback()
            raise HTTPException(status_code=500, detail="Database error")

    return {"msg": "user created"}


@app.post("/login")
@limiter.limit("5/minute")
def login(request: Request,data: UserLogin, db: Session = Depends(get_db)):

    user = db.query(User).filter(User.email == data.email).first()

    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token({"sub": str(user.id)})

    return {"access_token": token, "token_type": "bearer"}

@app.get("/me")
def get_me(user_id: str = Depends(get_current_user)):
    return {"message": f"You are logged in as user {user_id}"}

@app.post("/workspaces")
def create_workspace(
    data: WorkspaceCreate,
    user_id: str = Depends(get_current_user),
    db: Session = Depends(get_db)
):

    #  1️⃣ Duplicate workspace protection
    existing_workspace = db.query(Workspace).filter(
        Workspace.name == data.name
    ).first()

    if existing_workspace:
        raise HTTPException(status_code=400, detail="Workspace already exists")

    try:
        #  2️⃣ Create workspace
        workspace = Workspace(name=data.name)
        db.add(workspace)
        db.commit()
        db.refresh(workspace)

        #  3️⃣ Make creator admin
        member = WorkspaceMember(
            user_id=int(user_id),
            workspace_id=workspace.id,
            role="admin"
        )

        db.add(member)
        db.commit()

        return {"workspace_id": workspace.id}

    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Database error")



@app.post("/workspaces/{workspace_id}/join")
def join_workspace(
    workspace_id: int,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(get_current_user),
    db: Session = Depends(get_db)
):

    workspace = db.query(Workspace).filter(
        Workspace.id == workspace_id
    ).first()

    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    existing = db.query(WorkspaceMember).filter(
        WorkspaceMember.user_id == int(user_id),
        WorkspaceMember.workspace_id == workspace_id
    ).first()

    if existing:
        raise HTTPException(status_code=400, detail="Already a member")

    member = WorkspaceMember(
        user_id=int(user_id),
        workspace_id=workspace_id,
        role="member"
    )

    db.add(member)
    db.commit()

    #  Fetch user email
    user = db.query(User).filter(User.id == int(user_id)).first()

    #  Run background email AFTER response
    background_tasks.add_task(send_invite_email, user.email)

    return {"message": "Joined workspace"}


@app.delete("/workspaces/{workspace_id}")
def delete_workspace(
    workspace_id: int,
    user_id: str = Depends(get_current_user),
    db: Session = Depends(get_db)
):

    is_admin(int(user_id), workspace_id, db)

     #  First delete memberships
    db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace_id
    ).delete()

    #  Then delete workspace
    workspace = db.query(Workspace).filter(
        Workspace.id == workspace_id
    ).first()


    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    db.delete(workspace)
    db.commit()

    return {"message": "Workspace deleted"}


@app.post("/workspaces/{workspace_id}/tasks")
@limiter.limit("3/minute")
def create_task(
    request: Request,
    workspace_id: int,
    data: TaskCreate,
    user_id: str = Depends(get_current_user),
    db: Session = Depends(get_db)
):

    member = db.query(WorkspaceMember).filter(
        WorkspaceMember.user_id == int(user_id),
        WorkspaceMember.workspace_id == workspace_id
    ).first()

    if not member:
        raise HTTPException(status_code=403, detail="Not a workspace member")

    if member.role != "admin" and data.assigned_to != int(user_id):
        raise HTTPException(
            status_code=403,
            detail="Members can only assign tasks to themselves"
        )

    assigned_member = db.query(WorkspaceMember).filter(
        WorkspaceMember.user_id == data.assigned_to,
        WorkspaceMember.workspace_id == workspace_id
    ).first()

    if not assigned_member:
        raise HTTPException(
            status_code=400,
            detail="Assigned user is not a member of this workspace"
        )

    task = Task(
        title=data.title,
        description=data.description,
        workspace_id=workspace_id,
        assigned_to=data.assigned_to,
        status="todo"
    )

    db.add(task)
    db.flush()

    log = ActivityLog(
        user_id=int(user_id),
        action="create_task",
        entity="task",
        entity_id=task.id
    )

    db.add(log)
    db.commit()
    db.refresh(task)

    invalidate_task_cache(workspace_id)

    #  WebSocket broadcast
    asyncio.run(
    manager.broadcast(
        workspace_id,
        f"Task created: {task.title}")
    )

    return {"task_id": task.id}



@app.put("/tasks/{task_id}/status")
def update_task_status(
    task_id: int,
    status: TaskStatus,
    user_id: str = Depends(get_current_user),
    db: Session = Depends(get_db)
):

    task = db.query(Task).filter(Task.id == task_id).first()

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    member = db.query(WorkspaceMember).filter(
        WorkspaceMember.user_id == int(user_id),
        WorkspaceMember.workspace_id == task.workspace_id
    ).first()

    if not member:
        raise HTTPException(status_code=403, detail="Not authorized")

    #  STRICT RULE
    if member.role != "admin" and task.assigned_to != int(user_id):
        raise HTTPException(
            status_code=403,
            detail="Only assigned user or admin can update status"
        )

    task.status = status

    log = ActivityLog(
    user_id=int(user_id),
    action="update_task_status",
    entity="task",
    entity_id=task.id
)
    db.add(log)
    db.commit()

    invalidate_task_cache(task.workspace_id)

    return {"message": "Status updated"}

@app.get("/workspaces/{workspace_id}/tasks", response_model=list[TaskResponse])
def get_tasks(
    workspace_id: int,
    skip: int = 0,
    limit: int = 5,
    user_id: str = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    cache_key = f"workspace:{workspace_id}:tasks:{skip}:{limit}"

    cached_data = redis_client.get(cache_key)

    # ✅ If cache exists
    if cached_data:
        return json.loads(cached_data.decode())

    # ✅ Authorization check
    member = db.query(WorkspaceMember).filter(
        WorkspaceMember.user_id == int(user_id),
        WorkspaceMember.workspace_id == workspace_id
    ).first()

    if not member:
        raise HTTPException(status_code=403, detail="Not authorized")

    # ✅ Fetch tasks from DB
    tasks = db.query(Task).filter(
        Task.workspace_id == workspace_id
    ).offset(skip).limit(limit).all()

    # ✅ Convert tasks to serializable format
    task_data = [
        {
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "status": task.status,
            "workspace_id": task.workspace_id,
            "assigned_to": task.assigned_to
        }
        for task in tasks
    ]

    # ✅ Store in Redis
    redis_client.setex(cache_key, 60, json.dumps(task_data))

    return task_data

@app.delete("/tasks/{task_id}")
def delete_task(
    task_id: int,
    user_id: str = Depends(get_current_user),
    db: Session = Depends(get_db)
):

    task = db.query(Task).filter(Task.id == task_id).first()

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    member = db.query(WorkspaceMember).filter(
        WorkspaceMember.user_id == int(user_id),
        WorkspaceMember.workspace_id == task.workspace_id
    ).first()

    if not member or member.role != "admin":
        raise HTTPException(status_code=403, detail="Only admin can delete tasks")

    log = ActivityLog(
    user_id=int(user_id),
    action="delete_task",
    entity="task",
    entity_id=task.id
)
    db.add(log)
    db.delete(task)
    db.commit()

    invalidate_task_cache(task.workspace_id)

    return {"message": "Task deleted"}

@app.websocket("/ws/{workspace_id}")
async def websocket_endpoint(websocket: WebSocket, workspace_id: int):

    await manager.connect(workspace_id, websocket)

    try:
        while True:
            data = await websocket.receive_text()
            await manager.broadcast(workspace_id, f"Message: {data}")

    except:
        manager.disconnect(workspace_id, websocket)