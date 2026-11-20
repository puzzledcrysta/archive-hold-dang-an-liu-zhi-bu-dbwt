import json
from app.modules.runs.service import get
from app.modules.supplements.service import list_supplements, release_state
from .decisions import timeline


def bundle(run_id):
    history = timeline(run_id)
    run = get(run_id)
    # The export carries source line evidence, the applicable decision version,
    # late hold supplements and the current release gate.
    return json.dumps({'manifest':run['manifest'],'counts':run['counts'],'results':run['accepted'],
                       'quarantine':run['quarantine'],'suppressed':run['suppressed'],
                       'reconciliation':run['book'],'review':history,
                       'supplements':list_supplements(run_id),
                       'release':release_state(run_id)},ensure_ascii=False,indent=2)
