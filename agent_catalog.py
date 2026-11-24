"""Validated local catalogue of data agents; Google remains the access authority."""
import json
import re
from pathlib import Path


def load_catalog(path=None):
    source = Path(path) if path else Path(__file__).with_name("agents.json")
    items = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(items, list) or not items:
        raise ValueError("El catálogo de agentes está vacío o no es una lista")
    catalog = {}
    resources = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Entrada de agente no válida")
        agent_id = item.get("id")
        resource = item.get("resource")
        if not isinstance(agent_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{1,39}", agent_id):
            raise ValueError("Identificador de agente no válido")
        if agent_id in catalog:
            raise ValueError("Identificador de agente duplicado")
        if not isinstance(resource, str) or not re.fullmatch(
                r"projects/aad-analitica-oee/locations/eu/dataAgents/[A-Za-z0-9_-]+", resource):
            raise ValueError("Recurso de agente no válido")
        if resource in resources:
            raise ValueError("Recurso de agente duplicado")
        resources.add(resource)
        examples = item.get("examples", [])
        if not isinstance(examples, list):
            raise ValueError("Los ejemplos del agente deben ser una lista")
        catalog[agent_id] = {
            "id": agent_id,
            "name": str(item.get("name") or agent_id)[:80],
            "resource": resource,
            "description": str(item.get("description") or "")[:250],
            "examples": [str(x)[:160] for x in examples[:6]],
        }
    return catalog


CATALOG = load_catalog()
