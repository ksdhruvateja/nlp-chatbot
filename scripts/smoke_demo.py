"""Exercise a running demo over HTTP: python -m scripts.smoke_demo [base_url]."""
import sys
import time

import httpx


def main():
    with httpx.Client(base_url=sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000",
                      timeout=15) as client:
        def call(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        assert call("GET", "/health/ready")["database"]
        auth = call("POST", "/api/dev/login", data={"email": "demo@example.com"})
        client.headers["Authorization"] = f"Bearer {auth['token']}"
        props = call("GET", "/api/properties")
        assert props, "Seed the demo database first"
        request = call("POST", "/api/repair-requests", data={
            "property_id": props[0]["id"], "service_category": "plumbing",
            "description": "My toilet keeps running after flushing. The flapper leaks water into the bowl."})
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            request = call("GET", f"/api/repair-requests/{request['id']}")
            if request["result"]:
                break
            time.sleep(0.5)
        result = request["result"]
        assert result, f"No result: {request['status']} {request.get('error')}"
        assert result["outcome"] in {"diy_plan", "safety_warning", "clarification", "professional"}, result
        assert result.get("sources"), "Expected citations from seeded knowledge"
        print(f"Demo HTTP smoke passed: {result['outcome']}, {len(result['sources'])} sources")


if __name__ == "__main__":
    main()
