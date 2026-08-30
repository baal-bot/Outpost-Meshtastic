ALTER TABLE ai_interaction
ADD COLUMN rejected_evidence_refs TEXT NOT NULL DEFAULT '[]';

ALTER TABLE ai_interaction
ADD COLUMN evidence_rejection_reason TEXT;
