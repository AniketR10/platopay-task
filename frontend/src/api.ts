const RAW_BASE = (import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");
export const API_BASE = `${RAW_BASE}/api/v1`;

export type Merchant = { id: string; name: string; created_at: string };
export type BankAccount = {
  id: string;
  account_holder_name: string;
  account_number_masked: string;
  ifsc: string;
  is_active: boolean;
};
export type Balance = {
  available_paise: number;
  held_paise: number;
  total_credited_paise: number;
  total_debited_paise: number;
};
export type MerchantDetail = {
  merchant: Merchant;
  balance: Balance;
  bank_accounts: BankAccount[];
};
export type Payout = {
  id: string;
  merchant: string;
  bank_account: string;
  amount_paise: number;
  status: "PENDING" | "PROCESSING" | "COMPLETED" | "FAILED";
  attempts: number;
  last_attempted_at: string | null;
  failure_reason: string;
  created_at: string;
  updated_at: string;
};
export type LedgerEntry = {
  id: string;
  entry_type: "CREDIT" | "DEBIT";
  status: "HOLD" | "POSTED";
  amount_paise: number;
  payout: string | null;
  description: string;
  created_at: string;
};

async function jsonOrThrow<T>(res: Response): Promise<T> {
  const text = await res.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!res.ok) {
    const err = new Error(typeof body === "object" && body && "message" in body ? String((body as { message: unknown }).message) : `HTTP ${res.status}`);
    (err as { status?: number; body?: unknown }).status = res.status;
    (err as { status?: number; body?: unknown }).body = body;
    throw err;
  }
  return body as T;
}

export const api = {
  listMerchants: () => fetch(`${API_BASE}/merchants`).then(jsonOrThrow<Merchant[]>),
  merchantDetail: (id: string) => fetch(`${API_BASE}/merchants/${id}`).then(jsonOrThrow<MerchantDetail>),
  merchantPayouts: (id: string) => fetch(`${API_BASE}/merchants/${id}/payouts`).then(jsonOrThrow<Payout[]>),
  merchantLedger: (id: string) => fetch(`${API_BASE}/merchants/${id}/ledger`).then(jsonOrThrow<LedgerEntry[]>),
  createPayout: (opts: {
    merchantId: string;
    bankAccountId: string;
    amountPaise: number;
    idempotencyKey: string;
  }) =>
    fetch(`${API_BASE}/payouts`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": opts.idempotencyKey,
        "X-Merchant-Id": opts.merchantId,
      },
      body: JSON.stringify({
        amount_paise: opts.amountPaise,
        bank_account_id: opts.bankAccountId,
      }),
    }).then(jsonOrThrow<Payout>),
};

export function paiseToRupees(paise: number): string {
  const sign = paise < 0 ? "-" : "";
  const abs = Math.abs(paise);
  const rupees = Math.floor(abs / 100);
  const remainder = abs % 100;
  return `${sign}₹${rupees.toLocaleString("en-IN")}.${remainder.toString().padStart(2, "0")}`;
}
