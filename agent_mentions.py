"""Explicit agent selection for the workspace composer."""
import re


MENTION = re.compile(r"^\s*@([a-z][a-z0-9_-]{1,39})(?=\s|$)", re.IGNORECASE)


def select_agent(question, mode, catalog, allowed):
    if mode not in ("auto", "manual"):
        raise ValueError("Modo de selección de agentes no válido.")
    match = MENTION.match(question)
    if not match:
        if mode == "manual":
            raise ValueError("Elige un agente al principio del mensaje, por ejemplo @ventas.")
        return None, question
    agent_id = match.group(1).lower()
    if agent_id not in catalog:
        raise ValueError(f"No existe el agente @{agent_id}.")
    if agent_id not in allowed:
        raise PermissionError(f"No tienes acceso al agente @{agent_id}.")
    cleaned = question[match.end():].strip()
    if not cleaned:
        raise ValueError(f"Escribe una petición después de @{agent_id}.")
    return agent_id, cleaned
