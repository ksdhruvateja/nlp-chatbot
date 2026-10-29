"""Exercise a running demo over HTTP: python -m scripts.smoke_demo [base_url]."""
import argparse
import time

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", nargs="?", default="http://127.0.0.1:8000")
    parser.add_argument("--require-provider", choices=["gemini", "openai", "local", "demo"])
    args = parser.parse_args()
    with httpx.Client(base_url=args.base_url,
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
        if args.require_provider:
            assert result.get("provider") == args.require_provider, f"Unexpected provider: {result.get('provider')}"
        print(f"HTTP smoke passed: provider={result.get('provider')}, model={result.get('model')}, "
              f"outcome={result['outcome']}, {len(result['sources'])} sources")


if __name__ == "__main__":
    main()
