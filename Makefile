.PHONY: test lint image kind deploy break-% detect fix clean

test:
	pytest -q
lint:
	ruff check .
image:
	docker build -t incident-autopilot:dev .
kind:
	kind create cluster --name lab --config k8s/kind-config.yaml
deploy: image
	kind load docker-image incident-autopilot:dev --name lab
	kubectl apply -k k8s/overlays/kind
	kubectl -n demo rollout status deployment/web --timeout=180s
break-%:            # make break-memory_leak | break-crash | break-not_ready
	kubectl -n demo set env deployment/web FAULT_MODE=$*
detect:
	python -m remediate.cli -n demo --from-cluster || true
fix:
	python -m remediate.cli -n demo --from-cluster --apply || true
clean:
	kind delete cluster --name lab
