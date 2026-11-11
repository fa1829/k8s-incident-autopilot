import json
import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def make_pod(name="web-7d9f8c6b5-abcde", deployment="web", pod_hash="7d9f8c6b5", namespace="demo",
             container="app", memory="128Mi", restarts=0, state=None, last_state=None, ready=True):
    return {
        "metadata": {
            "name": name, "namespace": namespace,
            "labels": {"app": deployment, "pod-template-hash": pod_hash},
            "ownerReferences": [{"kind": "ReplicaSet", "name": f"{deployment}-{pod_hash}"}],
        },
        "spec": {"containers": [{"name": container, "resources": {"limits": {"memory": memory}}}]},
        "status": {"containerStatuses": [{
            "name": container, "ready": ready, "restartCount": restarts,
            "state": state or {"running": {"startedAt": "2026-01-01T00:00:00Z"}},
            "lastState": last_state or {},
        }]},
    }


@pytest.fixture
def pod():
    return make_pod


@pytest.fixture
def fixture_file():
    def load(name):
        return json.loads((FIXTURES / name).read_text())
    return load
