from app import logic

BODY = {"booking_id": "bk-1", "member_id": "m-1", "type": "BOOKING_CONFIRMED", "message": "Booked!"}


def test_sent_once_even_if_called_twice(client, monkeypatch):
    calls = []
    monkeypatch.setattr(logic, "send_sms", lambda to, text: calls.append(to) or True)
    first, second = client.post("/api/v1/notifications", json=BODY), client.post("/api/v1/notifications", json=BODY)
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["id"] == second.json()["id"] and len(calls) == 1


def test_provider_failure_is_recorded_not_raised(client, monkeypatch):
    monkeypatch.setattr(logic, "send_sms", lambda to, text: False)
    r = client.post("/api/v1/notifications", json=BODY)
    assert r.status_code == 201 and r.json()["state"] == "FAILED"


def test_validation_error_uses_common_format(client):
    r = client.post("/api/v1/notifications", json={**BODY, "type": "SPAM"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
