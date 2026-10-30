// Mirrors the gateway's /v2 response models (services/gateway/src/gateway/v2.py)
// and ledgerlens_common.models. Keep in sync when the API changes.

export type Role = 'analyst' | 'admin';

export interface ApiErrorBody {
  code: string;
  message: string;
  request_id: string | null;
}

export interface RiskReason {
  code: string; // VELOCITY | AMOUNT_ZSCORE | COUNTRY_JUMP
  detail: string;
}

export interface Risk {
  score: number;
  flagged: boolean;
  reasons: RiskReason[];
  scored_at: string;
}

export interface Money {
  amount: string; // exact decimal string, never parsed to a float for display
  currency: string;
}

export interface TxnV2 {
  txn_id: string;
  customer_id: string;
  merchant: string;
  category: string;
  money: Money;
  country: string;
  channel: string;
  timestamp: string;
  risk: Risk | null; // null = not scored yet
}

export interface PageV2 {
  data: TxnV2[];
  next_cursor: string | null;
}

export interface TxnFilter {
  customer_id?: string;
  merchant?: string;
  flagged?: boolean;
  from?: string; // ISO-8601
  to?: string;
}

export interface CustomerV2 {
  customer_id: string;
  recent_transactions: TxnV2[];
  risk_profile: Record<string, unknown> | null;
  risk_profile_error?: { code: string; message: string } | null;
}

/** The raw transaction inside a RiskAssessment (Kafka payload, not the v2 shape). */
export interface Transaction {
  txn_id: string;
  customer_id: string;
  merchant: string;
  category: string;
  amount: string;
  currency: string;
  country: string;
  channel: string;
  timestamp: string;
}

/** What the WebSocket streams: one message per flagged transaction. */
export interface RiskAssessment {
  txn_id: string;
  customer_id: string;
  score: number;
  flagged: boolean;
  reasons: RiskReason[];
  scored_at: string;
  transaction: Transaction;
}

export interface AgentAnswer {
  answer: string;
  terms_used: string[];
  pipeline: unknown[];
  rows: Record<string, unknown>[];
  allowed: boolean;
}

export interface AuditEntry {
  _id: string;
  user: string;
  role: string;
  question: string;
  pipeline: unknown;
  allowed: boolean;
  reason?: string;
  timestamp: string;
}
