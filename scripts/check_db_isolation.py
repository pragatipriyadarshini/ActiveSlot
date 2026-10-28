#!/usr/bin/env python3
"""Proves database-per-service with separate credentials: every service user can open its own database
and is refused by all the others.   python scripts/check_db_isolation.py"""
import os
import sys

import psycopg2

from _lib import Report

HOST, PORT = os.environ.get("PGHOST", "localhost"), os.environ.get("PGPORT", "5432")
USERS = {"schedule": "schedule_pw", "booking": "booking_pw", "payment": "payment_pw", "notification": "notification_pw"}
r = Report("tests", "db-isolation")
for user in USERS:
    for db in USERS:
        try:
            psycopg2.connect(host=HOST, port=PORT, user=f"{user}_user", password=USERS[user], dbname=f"{db}_db",
                             connect_timeout=3).close()
            outcome = "CONNECTED"
        except psycopg2.OperationalError as exc:
            outcome = "REFUSED  (" + str(exc).strip().splitlines()[-1][:60] + ")"
        expected = (user == db)
        r.check((outcome == "CONNECTED") == expected, f"{user}_user -> {db}_db : {outcome}")
sys.exit(r.finish())
