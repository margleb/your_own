-- Reference DDL for storage.py version 1. Runtime initializes these tables with
-- SQLAlchemy; knowledge.py owns its additional document/passage tables.
-- Use a dedicated database and role. This is not a migration for Your Own.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE pastoral_budget_days (
	day VARCHAR(10) NOT NULL,
	spent NUMERIC(18, 8) NOT NULL,
	reserved NUMERIC(18, 8) NOT NULL,
	PRIMARY KEY (day)
)

;

CREATE TABLE pastoral_daily_quotas (
	day VARCHAR(10) NOT NULL,
	user_id BIGINT NOT NULL,
	answered INTEGER NOT NULL,
	pending INTEGER NOT NULL,
	PRIMARY KEY (day, user_id)
)

;

CREATE TABLE pastoral_deletion_events (
	id VARCHAR(36) NOT NULL,
	user_id BIGINT NOT NULL,
	deleted_before TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	epoch INTEGER NOT NULL,
	PRIMARY KEY (id)
)

;

CREATE TABLE pastoral_telegram_receipts (
	update_id BIGINT NOT NULL,
	status VARCHAR(24) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (update_id)
)

;

CREATE TABLE pastoral_users (
	user_id BIGINT NOT NULL,
	mode VARCHAR(16) NOT NULL,
	conversation_id VARCHAR(36) NOT NULL,
	epoch INTEGER NOT NULL,
	campaign VARCHAR(64),
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	last_seen_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	first_answer_at TIMESTAMP WITHOUT TIME ZONE,
	last_answer_at TIMESTAMP WITHOUT TIME ZONE,
	returned BOOLEAN NOT NULL,
	PRIMARY KEY (user_id)
)

;

CREATE TABLE pastoral_budget_reservations (
	id VARCHAR(36) NOT NULL,
	day VARCHAR(10) NOT NULL,
	user_id BIGINT NOT NULL,
	amount NUMERIC(18, 8) NOT NULL,
	actual NUMERIC(18, 8),
	state VARCHAR(16) NOT NULL,
	answered BOOLEAN NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(day) REFERENCES pastoral_budget_days (day)
)

;

CREATE TABLE pastoral_conversations (
	id VARCHAR(36) NOT NULL,
	user_id BIGINT NOT NULL,
	mode VARCHAR(16) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES pastoral_users (user_id) ON DELETE CASCADE
)

;

CREATE TABLE pastoral_messages (
	id SERIAL NOT NULL,
	user_id BIGINT NOT NULL,
	conversation_id VARCHAR(36) NOT NULL,
	role VARCHAR(16) NOT NULL,
	content TEXT NOT NULL,
	reply_to_id INTEGER,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES pastoral_users (user_id) ON DELETE CASCADE,
	FOREIGN KEY(conversation_id) REFERENCES pastoral_conversations (id) ON DELETE CASCADE,
	FOREIGN KEY(reply_to_id) REFERENCES pastoral_messages (id) ON DELETE CASCADE
)

;

CREATE TABLE pastoral_dialogue_chunks (
	id SERIAL NOT NULL,
	user_id BIGINT NOT NULL,
	conversation_id VARCHAR(36) NOT NULL,
	user_message_id INTEGER NOT NULL,
	assistant_message_id INTEGER NOT NULL,
	embedding VECTOR(384) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES pastoral_users (user_id) ON DELETE CASCADE,
	FOREIGN KEY(conversation_id) REFERENCES pastoral_conversations (id) ON DELETE CASCADE,
	FOREIGN KEY(user_message_id) REFERENCES pastoral_messages (id) ON DELETE CASCADE,
	FOREIGN KEY(assistant_message_id) REFERENCES pastoral_messages (id) ON DELETE CASCADE
)

;

CREATE TABLE pastoral_jobs (
	update_id BIGINT NOT NULL,
	user_id BIGINT NOT NULL,
	chat_id BIGINT NOT NULL,
	conversation_id VARCHAR(36) NOT NULL,
	epoch INTEGER NOT NULL,
	mode VARCHAR(16) NOT NULL,
	message_id INTEGER NOT NULL,
	status VARCHAR(24) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	delivered_chunks INTEGER NOT NULL,
	PRIMARY KEY (update_id),
	FOREIGN KEY(update_id) REFERENCES pastoral_telegram_receipts (update_id) ON DELETE CASCADE,
	FOREIGN KEY(user_id) REFERENCES pastoral_users (user_id) ON DELETE CASCADE,
	FOREIGN KEY(conversation_id) REFERENCES pastoral_conversations (id) ON DELETE CASCADE,
	FOREIGN KEY(message_id) REFERENCES pastoral_messages (id) ON DELETE CASCADE
)

;

CREATE INDEX ix_pastoral_conversations_user_id ON pastoral_conversations (user_id);
CREATE INDEX ix_pastoral_messages_user_id ON pastoral_messages (user_id);
CREATE INDEX ix_pastoral_messages_conversation_id ON pastoral_messages (conversation_id);
CREATE INDEX ix_pastoral_dialogue_chunks_user_id ON pastoral_dialogue_chunks (user_id);
