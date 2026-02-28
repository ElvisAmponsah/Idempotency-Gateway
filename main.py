from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from pydantic import BaseModel
import asyncio
import time
import uuid
from contextlib import asynccontextmanager

# In-memory storage for idempotency keys.
# Structure: 
# {
#     "some-uuid-string": {
#         "request_payload": {"amount": 100.0, "currency": "USD"},
#         "response": {"status": "Charged 100.0 USD"}, # Only present if completed
#         "status_code": 201,                        # Only present if completed
#         "event": asyncio.Event(),                  # Used to block concurrent requests
#         "created_at": 1690000000.0                 # Timestamp for expiry/cleanup
#     }
# }
idempotency_store = {}

# --- Developer's Choice: Key Expiry (User Story 7) ---
# To prevent the in-memory dictionary from growing indefinitely (memory leak),
# we implement a simple background task that runs periodically to remove keys 
# older than a certain threshold (e.g., 24 hours).

EXPIRY_SECONDS = 24 * 60 * 60  # 24 hours
CLEANUP_INTERVAL_SECONDS = 60 * 60  # Run cleanup every hour

async def cleanup_expired_keys():
    """Background task to remove expired idempotency keys from memory."""
    try:
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            current_time = time.time()
            # Find keys that have expired
            keys_to_delete = [
                key for key, data in idempotency_store.items()
                if current_time - data["created_at"] > EXPIRY_SECONDS
            ]
            # Delete the expired keys
            for key in keys_to_delete:
                del idempotency_store[key]
            if keys_to_delete:
                print(f"Cleaned up {len(keys_to_delete)} expired idempotency keys.")
    except asyncio.CancelledError:
        # Task was cancelled gracefully during application shutdown
        pass
# -----------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager to handle background tasks on startup/shutdown."""
    # Startup: Start the background cleanup task
    cleanup_task = asyncio.create_task(cleanup_expired_keys())
    yield
    # Shutdown: Clean up the task gracefully
    cleanup_task.cancel()

app = FastAPI(
    title="Idempotency-Gateway",
    description="A simple FastAPI gateway demonstrating idempotency.",
    version="1.0.0",
    lifespan=lifespan
)

class PaymentRequest(BaseModel):
    amount: float
    currency: str

@app.post("/process-payment", status_code=status.HTTP_201_CREATED)
async def process_payment(
    payment_request: PaymentRequest,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key", description="Unique key for idempotent requests")
):
    """
    Process a payment idempotently.
    
    Headers:
        Idempotency-Key: A completely unique string (like a UUID) for this payment intent.
    """
    payload_dict = payment_request.dict()

    # Check if we have seen this key before
    if idempotency_key in idempotency_store:
        cached_data = idempotency_store[idempotency_key]
        
        # US3 (Conflict): Same key, but different request body
        if cached_data["request_payload"] != payload_dict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency key already used for a different request body."
            )

        # US6 (In-Flight / Race Condition): 
        # Same key and same body, but the original request is STILL processing.
        # We wait for the 'event' to be set by the first request.
        if "response" not in cached_data:
            # This blocks execution here until `cached_data["event"].set()` is called by Request A.
            await cached_data["event"].wait()
            
            # STRICT REVIEW FIX: If Request A crashed with an exception, it still triggered
            # `event.set()` in its finally block to prevent Request B from hanging infinitely.
            # However, Request A failed to save the "response". We must catch this edge case!
            if "response" not in cached_data:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="The original request failed during processing. Please try again."
                )
            
        # At this point, the original request has finished processing gracefully.
        # US2 (Duplicate): Exact same request and key. Return saved response.
        response.status_code = cached_data["status_code"]
        response.headers["X-Cache-Hit"] = "true"
        return cached_data["response"]

    # First time seeing this key. We must initialize it in the store to
    # signify that processing is "in-flight".
    
    # We create an asyncio.Event. It starts in an 'unset' (blocking) state.
    completion_event = asyncio.Event()
    
    idempotency_store[idempotency_key] = {
        "request_payload": payload_dict,
        "event": completion_event,
        "created_at": time.time()
    }

    try:
        # US1 (Happy Path): Simulate standard processing delay
        await asyncio.sleep(2)
        
        # Generate the successful response payload
        success_response = {
            "status": f"Charged {payment_request.amount} {payment_request.currency}"
        }
        success_status_code = status.HTTP_201_CREATED
        
        # Save the result to the cache so subsequent requests can retrieve it
        idempotency_store[idempotency_key]["response"] = success_response
        idempotency_store[idempotency_key]["status_code"] = success_status_code
        
        response.status_code = success_status_code
        return success_response
        
    finally:
        # CRITICAL: No matter what happens (success or an unexpected exception during processing),
        # we MUST set the event. 
        # Setting the event releases ANY AND ALL other requests (Request B, C, etc.)
        # that are currently waiting on `await cached_data["event"].wait()` in US6.
        completion_event.set()
