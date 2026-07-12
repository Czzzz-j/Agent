-- 鍒涘缓鏁版嵁搴擄紙濡傛灉涓嶅瓨鍦級
CREATE DATABASE IF NOT EXISTS furniture_agent CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE furniture_agent;

-- 鐢ㄦ埛琛?
CREATE TABLE IF NOT EXISTS `users` (
    `id` INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    `uuid` CHAR(36) NOT NULL UNIQUE,
    `username` VARCHAR(50) UNIQUE,
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    `last_active` TIMESTAMP,
    INDEX idx_uuid (uuid)
);

-- 瀵硅瘽浼氳瘽琛?
CREATE TABLE IF NOT EXISTS `chat_sessions` (
    `id` BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    `session_id` CHAR(36) NOT NULL,
    `user_uuid` CHAR(36) NOT NULL,
    `title` VARCHAR(200),
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_session_id (session_id),
    INDEX idx_user (user_uuid),
    FOREIGN KEY (user_uuid) REFERENCES users(uuid) ON DELETE CASCADE
);

-- 娑堟伅鍘嗗彶琛?
CREATE TABLE IF NOT EXISTS `chat_messages` (
    `id` BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    `session_id` CHAR(36) NOT NULL,
    `role` ENUM('user', 'assistant', 'system') NOT NULL,
    `content` TEXT NOT NULL,
    `request_id` VARCHAR(64) NULL,
    `sequence_no` TINYINT UNSIGNED NOT NULL DEFAULT 0,
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_session_time (session_id, created_at),
    INDEX idx_session_time_id (session_id, created_at, id),
    UNIQUE KEY uk_session_request_sequence (session_id, request_id, sequence_no),
    FOREIGN KEY (session_id) REFERENCES chat_sessions(session_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS `session_memory` (
    `session_id` CHAR(36) PRIMARY KEY,
    `summary_json` JSON NOT NULL,
    `source_message_upto_id` BIGINT UNSIGNED NOT NULL,
    `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES chat_sessions(session_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS `user_memory` (
    `id` BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    `user_uuid` CHAR(36) NOT NULL,
    `memory_key` VARCHAR(64) NOT NULL,
    `memory_value` JSON NOT NULL,
    `confidence` DECIMAL(4,2) NOT NULL DEFAULT 0.90,
    `source_session_id` CHAR(36) NOT NULL,
    `source_message_id` BIGINT UNSIGNED NOT NULL,
    `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_user_memory_key (user_uuid, memory_key),
    INDEX idx_user_memory_updated_at (user_uuid, updated_at),
    FOREIGN KEY (user_uuid) REFERENCES users(uuid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS `memory_index_outbox` (
    `id` BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    `task_type` VARCHAR(64) NOT NULL,
    `aggregate_id` VARCHAR(160) NOT NULL,
    `session_id` CHAR(36) NOT NULL,
    `user_uuid` CHAR(36) NOT NULL,
    `status` ENUM('pending', 'processing', 'completed', 'dead') NOT NULL DEFAULT 'pending',
    `retry_count` INT UNSIGNED NOT NULL DEFAULT 0,
    `next_retry_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `locked_at` TIMESTAMP NULL,
    `last_error` TEXT NULL,
    `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY `uk_outbox_task_aggregate` (`task_type`, `aggregate_id`),
    INDEX `idx_outbox_pending` (`status`, `next_retry_at`),
    INDEX `idx_outbox_locked` (`status`, `locked_at`),
    FOREIGN KEY (`session_id`) REFERENCES `chat_sessions`(`session_id`) ON DELETE CASCADE,
    FOREIGN KEY (`user_uuid`) REFERENCES `users`(`uuid`) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS `knowledge_documents` (
    `id` BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    `document_id` CHAR(36) NOT NULL,
    `source_name` VARCHAR(255) NOT NULL,
    `source_path` VARCHAR(512) NOT NULL,
    `file_type` VARCHAR(16) NOT NULL,
    `domain` VARCHAR(64) NOT NULL DEFAULT 'general',
    `active_version_id` CHAR(36) NULL,
    `is_deleted` TINYINT(1) NOT NULL DEFAULT 0,
    `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY `uk_knowledge_document_id` (`document_id`),
    UNIQUE KEY `uk_knowledge_source_path` (`source_path`),
    INDEX `idx_knowledge_active_version` (`active_version_id`)
);

CREATE TABLE IF NOT EXISTS `knowledge_document_versions` (
    `id` BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    `version_id` CHAR(36) NOT NULL,
    `document_id` CHAR(36) NOT NULL,
    `version_no` INT UNSIGNED NOT NULL,
    `content_sha256` CHAR(64) NOT NULL,
    `category` VARCHAR(64) NOT NULL DEFAULT 'general',
    `parser_version` VARCHAR(32) NOT NULL,
    `embedding_version` VARCHAR(64) NOT NULL,
    `chunk_count` INT UNSIGNED NOT NULL DEFAULT 0,
    `status` ENUM('pending', 'processing', 'active', 'superseded', 'deleted', 'failed') NOT NULL DEFAULT 'pending',
    `source_snapshot_path` VARCHAR(512) NOT NULL,
    `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY `uk_knowledge_version_id` (`version_id`),
    UNIQUE KEY `uk_document_sha256` (`document_id`, `content_sha256`),
    INDEX `idx_knowledge_version_status` (`status`, `updated_at`),
    CONSTRAINT `fk_knowledge_version_document` FOREIGN KEY (`document_id`) REFERENCES `knowledge_documents`(`document_id`) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS `knowledge_chunks` (
    `id` BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    `chunk_id` VARCHAR(191) NOT NULL,
    `document_id` CHAR(36) NOT NULL,
    `version_id` CHAR(36) NOT NULL,
    `chunk_index` INT UNSIGNED NOT NULL,
    `content` MEDIUMTEXT NOT NULL,
    `keywords_json` JSON NULL,
    `metadata_json` JSON NOT NULL,
    `content_hash` CHAR(64) NOT NULL,
    `is_active` TINYINT(1) NOT NULL DEFAULT 0,
    `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY `uk_knowledge_chunk_id` (`chunk_id`),
    UNIQUE KEY `uk_version_chunk_index` (`version_id`, `chunk_index`),
    INDEX `idx_knowledge_chunk_active` (`is_active`, `document_id`),
    INDEX `idx_knowledge_chunk_version` (`version_id`, `is_active`),
    CONSTRAINT `fk_knowledge_chunk_document` FOREIGN KEY (`document_id`) REFERENCES `knowledge_documents`(`document_id`) ON DELETE CASCADE,
    CONSTRAINT `fk_knowledge_chunk_version` FOREIGN KEY (`version_id`) REFERENCES `knowledge_document_versions`(`version_id`) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS `knowledge_index_outbox` (
    `id` BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    `task_type` VARCHAR(32) NOT NULL,
    `aggregate_id` VARCHAR(191) NOT NULL,
    `document_id` CHAR(36) NULL,
    `version_id` CHAR(36) NULL,
    `source_path` VARCHAR(512) NULL,
    `status` ENUM('pending', 'processing', 'completed', 'dead') NOT NULL DEFAULT 'pending',
    `retry_count` INT UNSIGNED NOT NULL DEFAULT 0,
    `next_retry_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `locked_at` TIMESTAMP NULL,
    `last_error` TEXT NULL,
    `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY `uk_knowledge_task_aggregate` (`task_type`, `aggregate_id`),
    INDEX `idx_knowledge_outbox_pending` (`status`, `next_retry_at`),
    INDEX `idx_knowledge_outbox_locked` (`status`, `locked_at`),
    CONSTRAINT `fk_knowledge_outbox_document` FOREIGN KEY (`document_id`) REFERENCES `knowledge_documents`(`document_id`) ON DELETE CASCADE,
    CONSTRAINT `fk_knowledge_outbox_version` FOREIGN KEY (`version_id`) REFERENCES `knowledge_document_versions`(`version_id`) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS `knowledge_index_state` (
    `state_key` VARCHAR(32) PRIMARY KEY,
    `collection_name` VARCHAR(128) NOT NULL,
    `generation` BIGINT UNSIGNED NOT NULL DEFAULT 1,
    `health_status` ENUM('ready', 'empty', 'stale', 'degraded') NOT NULL DEFAULT 'empty',
    `active_documents` INT UNSIGNED NOT NULL DEFAULT 0,
    `active_chunks` INT UNSIGNED NOT NULL DEFAULT 0,
    `last_sync_at` TIMESTAMP NULL,
    `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `conversation_tasks` (
    `id` BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    `task_id` CHAR(36) NOT NULL,
    `user_uuid` CHAR(36) NOT NULL,
    `origin_session_id` CHAR(36) NOT NULL,
    `active_session_id` CHAR(36) NOT NULL,
    `origin_request_id` VARCHAR(64) NULL,
    `topic` VARCHAR(200) NOT NULL,
    `subject_type` VARCHAR(64) NULL,
    `status` ENUM('active', 'paused', 'resolved', 'abandoned') NOT NULL DEFAULT 'active',
    `goal` TEXT NULL,
    `next_action` TEXT NULL,
    `state_version` INT UNSIGNED NOT NULL DEFAULT 1,
    `last_message_id` BIGINT UNSIGNED NOT NULL,
    `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY `uk_conversation_task_id` (`task_id`),
    UNIQUE KEY `uk_task_origin_request` (`user_uuid`, `origin_request_id`),
    INDEX `idx_task_user_status_updated` (`user_uuid`, `status`, `updated_at`),
    INDEX `idx_task_session_status` (`active_session_id`, `status`),
    FOREIGN KEY (`user_uuid`) REFERENCES `users`(`uuid`) ON DELETE CASCADE,
    FOREIGN KEY (`origin_session_id`) REFERENCES `chat_sessions`(`session_id`) ON DELETE CASCADE,
    FOREIGN KEY (`active_session_id`) REFERENCES `chat_sessions`(`session_id`) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS `conversation_task_facts` (
    `id` BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    `task_id` CHAR(36) NOT NULL,
    `fact_type` ENUM(
        'confirmed_fact',
        'constraint',
        'attempt',
        'result',
        'rejection',
        'open_question'
    ) NOT NULL,
    `fact_value_json` JSON NOT NULL,
    `status` ENUM('active', 'superseded', 'retracted') NOT NULL DEFAULT 'active',
    `confidence` DECIMAL(4,2) NOT NULL DEFAULT 0.90,
    `source_message_id` BIGINT UNSIGNED NOT NULL,
    `request_id` VARCHAR(64) NOT NULL,
    `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY `uk_task_fact_request` (`task_id`, `fact_type`, `request_id`),
    INDEX `idx_task_fact_active` (`task_id`, `status`, `fact_type`),
    FOREIGN KEY (`task_id`) REFERENCES `conversation_tasks`(`task_id`) ON DELETE CASCADE,
    FOREIGN KEY (`source_message_id`) REFERENCES `chat_messages`(`id`) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS `conversation_task_events` (
    `id` BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    `task_id` CHAR(36) NOT NULL,
    `request_id` VARCHAR(64) NOT NULL,
    `source_message_id` BIGINT UNSIGNED NOT NULL,
    `patch_json` JSON NOT NULL,
    `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY `uk_task_event_request` (`task_id`, `request_id`),
    FOREIGN KEY (`task_id`) REFERENCES `conversation_tasks`(`task_id`) ON DELETE CASCADE,
    FOREIGN KEY (`source_message_id`) REFERENCES `chat_messages`(`id`) ON DELETE CASCADE
);

-- user feedback table
CREATE TABLE IF NOT EXISTS `feedbacks` (
    `id` BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    `user_uuid` CHAR(36) NOT NULL,
    `session_id` CHAR(36) NULL,
    `user_question` TEXT NOT NULL,
    `assistant_answer` TEXT NOT NULL,
    `feedback_type` ENUM('dislike', 'like') DEFAULT 'dislike',
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_user (user_uuid),
    FOREIGN KEY (user_uuid) REFERENCES users(uuid)
);

-- 澶栭儴浣跨敤璁板綍琛?
CREATE TABLE IF NOT EXISTS `external_records` (
    `id` BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    `user_id` VARCHAR(20) NOT NULL,
    `month` CHAR(7) NOT NULL,
    `feature` VARCHAR(100),
    `efficiency` VARCHAR(100),
    `consumables` VARCHAR(100),
    `comparison` VARCHAR(100),
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_user_month (user_id, month)
);
