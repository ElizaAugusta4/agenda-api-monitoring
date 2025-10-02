from fastapi import FastAPI, HTTPException, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import uuid
from datetime import datetime
import psutil
import time
import logging
import json
from prometheus_fastapi_instrumentator import Instrumentator

# OpenTelemetry imports
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION

logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp": "%(asctime)s", "level": "%(levelname)s", "message": "%(message)s", "module": "%(name)s"}',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("agenda-api")

resource = Resource(attributes={
    SERVICE_NAME: "agenda-api",
    SERVICE_VERSION: "1.0.0"
})

trace.set_tracer_provider(TracerProvider(resource=resource))
tracer = trace.get_tracer(__name__)

otlp_exporter = OTLPSpanExporter(endpoint="http://jaeger:4317", insecure=True)
span_processor = BatchSpanProcessor(otlp_exporter)
trace.get_tracer_provider().add_span_processor(span_processor)

app = FastAPI(title="Agenda API", description="API simples para gerenciar contatos", version="1.0.0")

FastAPIInstrumentor.instrument_app(app)

instrumentator = Instrumentator()
instrumentator.instrument(app)
instrumentator.expose(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    
    logger.info(json.dumps({
        "event": "request_started",
        "method": request.method,
        "url": str(request.url),
        "client_ip": request.client.host if request.client else "unknown"
    }))
    
    response = await call_next(request)
    
    process_time = time.time() - start_time
    logger.info(json.dumps({
        "event": "request_completed",
        "method": request.method,
        "url": str(request.url),
        "status_code": response.status_code,
        "process_time": round(process_time, 4)
    }))
    
    return response

class ContactCreate(BaseModel):
    nome: str
    telefone: str
    email: Optional[str] = None
    endereco: Optional[str] = None

class Contact(BaseModel):
    id: str
    nome: str
    telefone: str
    email: Optional[str] = None
    endereco: Optional[str] = None
    created_at: datetime

contacts_db = []

@app.get("/")
async def root():
    return {
        "message": "Bem-vindo à API de Agenda!", 
        "status": "active",
        "endpoints": {
            "adicionar_contato": "POST /contatos",
            "listar_contatos": "GET /contatos",
            "buscar_contato": "GET /contatos/{contact_id}",
            "health_check": "GET /health",
            "system_metrics": "GET /system-metrics",
            "prometheus_metrics": "GET /metrics"
        }
    }

@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "agenda-api", "total_contatos": len(contacts_db)}

@app.get("/system-metrics")
async def system_metrics():
    cpu_percent = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
    return {
        "timestamp": datetime.now().isoformat(),
        "cpu": {
            "usage_percent": cpu_percent,
            "count": psutil.cpu_count()
        },
        "memory": {
            "total_gb": round(memory.total / (1024**3), 2),
            "available_gb": round(memory.available / (1024**3), 2),
            "used_gb": round(memory.used / (1024**3), 2),
            "usage_percent": memory.percent
        },
        "disk": {
            "total_gb": round(disk.total / (1024**3), 2),
            "used_gb": round(disk.used / (1024**3), 2),
            "free_gb": round(disk.free / (1024**3), 2),
            "usage_percent": round((disk.used / disk.total) * 100, 2)
        },
        "contacts_count": len(contacts_db)
    }

@app.get("/system-metrics-prometheus")
async def system_metrics_prometheus():
    """Endpoint que retorna métricas do sistema no formato Prometheus"""
    cpu_percent = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
    # Formato Prometheus: metric_name{labels} value timestamp
    metrics = []
    metrics.append(f"system_cpu_usage_percent {cpu_percent}")
    metrics.append(f"system_cpu_count {psutil.cpu_count()}")
    metrics.append(f"system_memory_total_bytes {memory.total}")
    metrics.append(f"system_memory_available_bytes {memory.available}")
    metrics.append(f"system_memory_used_bytes {memory.used}")
    metrics.append(f"system_memory_usage_percent {memory.percent}")
    metrics.append(f"system_disk_total_bytes {disk.total}")
    metrics.append(f"system_disk_used_bytes {disk.used}")
    metrics.append(f"system_disk_free_bytes {disk.free}")
    metrics.append(f"system_disk_usage_percent {round((disk.used / disk.total) * 100, 2)}")
    metrics.append(f"agenda_contacts_total {len(contacts_db)}")
    
    return Response(content="\n".join(metrics) + "\n", media_type="text/plain")

@app.post("/contatos", response_model=Contact)
async def adicionar_contato(contato: ContactCreate):
    with tracer.start_as_current_span("create_contact") as span:
        # Adiciona atributos ao span
        span.set_attribute("contact.name", contato.nome)
        span.set_attribute("contact.has_email", contato.email is not None)
        span.set_attribute("contact.has_address", contato.endereco is not None)
        
        logger.info(json.dumps({
            "event": "creating_contact",
            "contact_name": contato.nome,
            "has_email": contato.email is not None,
            "has_address": contato.endereco is not None
        }))
        
        with tracer.start_as_current_span("generate_contact_id"):
            contact_id = str(uuid.uuid4())
        
        with tracer.start_as_current_span("create_contact_object"):
            new_contact = Contact(
                id=contact_id,
                nome=contato.nome,
                telefone=contato.telefone,
                email=contato.email,
                endereco=contato.endereco,
                created_at=datetime.now()
            )
        
        with tracer.start_as_current_span("save_contact_to_db"):
            contacts_db.append(new_contact)
            span.set_attribute("total_contacts", len(contacts_db))
        
        logger.info(json.dumps({
            "event": "contact_created",
            "contact_id": new_contact.id,
            "contact_name": new_contact.nome,
            "total_contacts": len(contacts_db)
        }))
        
        return new_contact

@app.get("/contatos", response_model=List[Contact])
async def visualizar_contatos():
    logger.info(json.dumps({
        "event": "listing_contacts",
        "total_contacts": len(contacts_db)
    }))
    return contacts_db

@app.get("/contatos/{contact_id}", response_model=Contact)
async def buscar_contato(contact_id: str):
    with tracer.start_as_current_span("search_contact") as span:
        span.set_attribute("contact.id", contact_id)
        span.set_attribute("database.size", len(contacts_db))
        
        logger.info(json.dumps({
            "event": "searching_contact",
            "contact_id": contact_id
        }))
        
        with tracer.start_as_current_span("database_search"):
            for contact in contacts_db:
                if contact.id == contact_id:
                    span.set_attribute("contact.found", True)
                    span.set_attribute("contact.name", contact.nome)
                    
                    logger.info(json.dumps({
                        "event": "contact_found",
                        "contact_id": contact_id,
                        "contact_name": contact.nome
                    }))
                    return contact
        
        span.set_attribute("contact.found", False)
        logger.warning(json.dumps({
            "event": "contact_not_found",
            "contact_id": contact_id
        }))
        raise HTTPException(status_code=404, detail="Contato não encontrado")