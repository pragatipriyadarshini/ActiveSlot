-- Database-per-service: four databases, four users, no shared credentials.
-- Each user owns exactly one database and cannot even connect to the others.
CREATE USER schedule_user     WITH PASSWORD 'schedule_pw';
CREATE USER booking_user      WITH PASSWORD 'booking_pw';
CREATE USER payment_user      WITH PASSWORD 'payment_pw';
CREATE USER notification_user WITH PASSWORD 'notification_pw';

CREATE DATABASE schedule_db     OWNER schedule_user;
CREATE DATABASE booking_db      OWNER booking_user;
CREATE DATABASE payment_db      OWNER payment_user;
CREATE DATABASE notification_db OWNER notification_user;

REVOKE CONNECT ON DATABASE schedule_db     FROM PUBLIC;
REVOKE CONNECT ON DATABASE booking_db      FROM PUBLIC;
REVOKE CONNECT ON DATABASE payment_db      FROM PUBLIC;
REVOKE CONNECT ON DATABASE notification_db FROM PUBLIC;
