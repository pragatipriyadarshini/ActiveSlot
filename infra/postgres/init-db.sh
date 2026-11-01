#!/bin/bash
set -euo pipefail
create_service_db() {   # $1 = service name   $2 = password
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
CREATE USER ${1}_user WITH PASSWORD '${2}';
CREATE DATABASE ${1}_db OWNER ${1}_user;
REVOKE CONNECT ON DATABASE ${1}_db FROM PUBLIC;
SQL
}
create_service_db schedule     "$SCHEDULE_DB_PASSWORD"
create_service_db booking      "$BOOKING_DB_PASSWORD"
create_service_db payment      "$PAYMENT_DB_PASSWORD"
create_service_db notification "$NOTIFICATION_DB_PASSWORD"