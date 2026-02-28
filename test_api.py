import asyncio
import httpx
import time

API_URL = "http://localhost:8000/process-payment"

async def make_request(request_id: int, key: str, payload: dict):
    """Fires a single payment request and returns its details."""
    print(f"[{time.strftime('%H:%M:%S')}] Request {request_id} launched. Key: {key}")
    
    # Use httpx.AsyncClient for non-blocking HTTP requests
    async with httpx.AsyncClient() as client:
        start_time = time.time()
        
        response = await client.post(
            API_URL, 
            json=payload, 
            headers={"Idempotency-Key": key},
            timeout=10.0 # Prevent script from hanging indefinitely 
        )
        
        end_time = time.time()
        duration = end_time - start_time
        
        # Format the output for proof of execution
        print(f"[{time.strftime('%H:%M:%S')}] Request {request_id} finished in {duration:.2f}s")
        print(f"    Status: {response.status_code}")
        print(f"    Body: {response.json()}")
        print(f"    X-Cache-Hit: {response.headers.get('X-Cache-Hit', 'False')}")
        print("-" * 40)

async def main():
    print("=== FinSafe Idempotency Gateway - Automated QA Test ===")
    
    # ---------------------------------------------------------
    # TEST 1: The Race Condition (US6)
    # ---------------------------------------------------------
    # We will fire 3 absolute concurrent requests using the exact same Idempotency-Key
    # and payload.
    # PROOF EXPECTATION:
    # 1. Request 1 should take ~2.0 seconds to finish. It will NOT have X-Cache-Hit.
    # 2. Requests 2 & 3 should wait exactly as long as Request 1 takes. 
    # 3. Requests 2 & 3 must finish at the EXACT SAME TIME as Request 1.
    # 4. Requests 2 & 3 MUST have X-Cache-Hit: true.
    # 5. ALL three must return status 201 Created.
    
    print("\n--- Starting Race Condition Test (3 concurrent identical requests) ---")
    race_key = "concurrent-test-key-001"
    race_payload = {"amount": 500.0, "currency": "USD"}
    
    # asyncio.gather fires all 3 coroutines at the exact same moment
    await asyncio.gather(
        make_request(1, race_key, race_payload),
        make_request(2, race_key, race_payload),
        make_request(3, race_key, race_payload)
    )

    # ---------------------------------------------------------
    # TEST 2: The Fraud / Conflict Check (US3)
    # ---------------------------------------------------------
    # We submit the exact same key we just used, but change the amount to $900.
    # PROOF EXPECTATION: 
    # 1. Should finish instantly (0.01s).
    # 2. Must return 409 Conflict.
    print("\n--- Starting Conflict Test (Same key, mapped to different payload) ---")
    fraud_payload = {"amount": 900.0, "currency": "USD"}
    await make_request(4, race_key, fraud_payload)

if __name__ == "__main__":
    asyncio.run(main())
