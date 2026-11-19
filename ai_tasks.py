"""
Tâches IA en arrière-plan.

Une réponse d'IA peut prendre plusieurs minutes sur un ordinateur sans carte
graphique. Plutôt que de garder une requête web ouverte tout ce temps (que le
navigateur ou le réseau peuvent couper), on lance la tâche ici et la page
demande toutes les 1,5 s si le résultat est prêt.
"""
import threading
import time
import uuid

import ai_client

MAX_TASKS = 50             # on garde les dernières tâches en mémoire
_tasks: dict[str, dict] = {}
_lock = threading.Lock()


def start(fn) -> str:
    """Lance fn() dans un thread ; renvoie l'identifiant de la tâche."""
    task_id = uuid.uuid4().hex[:12]
    with _lock:
        _tasks[task_id] = {"status": "running", "started": time.time(), "result": None, "error": None}
        for old in list(_tasks)[:-MAX_TASKS]:
            _tasks.pop(old, None)

    def run():
        try:
            result, error = fn(), None
        except ai_client.AiError as e:
            result, error = None, str(e)
        except Exception as e:      # erreur imprévue : on la montre plutôt que de bloquer la page
            result, error = None, f"Erreur inattendue : {e}"
        with _lock:
            task = _tasks.get(task_id)
            if task:
                task.update(status="error" if error else "done", result=result, error=error)

    threading.Thread(target=run, daemon=True, name=f"ai-task-{task_id}").start()
    return task_id


def get(task_id: str) -> dict | None:
    with _lock:
        task = _tasks.get(task_id)
        if not task:
            return None
        return {"status": task["status"], "result": task["result"], "error": task["error"],
                "elapsed": round(time.time() - task["started"])}
