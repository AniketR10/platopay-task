import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  paiseToRupees,
  type BankAccount,
  type LedgerEntry,
  type Merchant,
  type MerchantDetail,
  type Payout,
} from "./api";

const POLL_MS = 2000;

type ToastKind = "success" | "error";
type Toast = { kind: ToastKind; text: string } | null;

export default function App() {
  const [merchants, setMerchants] = useState<Merchant[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<MerchantDetail | null>(null);
  const [payouts, setPayouts] = useState<Payout[]>([]);
  const [ledger, setLedger] = useState<LedgerEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<Toast>(null);

  useEffect(() => {
    api.listMerchants()
      .then((rows) => {
        setMerchants(rows);
        if (rows.length && !selected) setSelected(rows[0].id);
      })
      .catch((e: Error) => setError(`Failed to load merchants: ${e.message}`));
  }, [selected]);

  const refresh = useCallback(async () => {
    if (!selected) return;
    try {
      const [d, p, l] = await Promise.all([
        api.merchantDetail(selected),
        api.merchantPayouts(selected),
        api.merchantLedger(selected),
      ]);
      setDetail(d);
      setPayouts(p);
      setLedger(l);
      setError(null);
    } catch (e) {
      setError(`Failed to refresh: ${(e as Error).message}`);
    }
  }, [selected]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, POLL_MS);
    return () => clearInterval(id);
  }, [refresh]);

  useEffect(() => {
    if (!toast) return;
    const id = setTimeout(() => setToast(null), 4000);
    return () => clearTimeout(id);
  }, [toast]);

  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto max-w-6xl px-6 py-4 flex items-center justify-between gap-4">
          <div>
            <h1 className="text-xl font-semibold tracking-tight">Playto Payouts</h1>
            <p className="text-xs text-slate-500">Merchant balance &amp; payout dashboard</p>
          </div>
          <select
            className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-slate-500 focus:outline-none"
            value={selected ?? ""}
            onChange={(e) => setSelected(e.target.value)}
          >
            {merchants.length === 0 && <option value="">(no merchants)</option>}
            {merchants.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
              </option>
            ))}
          </select>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-6 py-6 space-y-6">
        {error && <Banner kind="error">{error}</Banner>}
        {toast && <Banner kind={toast.kind}>{toast.text}</Banner>}

        {detail && <BalanceCard detail={detail} />}

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {detail && (
            <PayoutForm
              merchantId={detail.merchant.id}
              bankAccounts={detail.bank_accounts}
              availablePaise={detail.balance.available_paise}
              onResult={(t) => {
                setToast(t);
                refresh();
              }}
            />
          )}

          <Section title="Recent payouts" hint="updates every 2s">
            <PayoutTable rows={payouts} />
          </Section>
        </div>

        <Section title="Ledger (last 100)" hint="append-only money movement log">
          <LedgerTable rows={ledger} />
        </Section>

        <p className="text-xs text-slate-400 text-center pt-4">
          Polling every {POLL_MS / 1000}s · Built for Playto Pay take-home
        </p>
      </main>
    </div>
  );
}

function Banner({ kind, children }: { kind: ToastKind; children: React.ReactNode }) {
  const cls =
    kind === "success"
      ? "bg-emerald-50 text-emerald-800 border-emerald-200"
      : "bg-rose-50 text-rose-800 border-rose-200";
  return <div className={`rounded-md border px-4 py-3 text-sm ${cls}`}>{children}</div>;
}

function Section({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white shadow-sm">
      <div className="px-4 py-3 border-b border-slate-200 flex items-baseline justify-between">
        <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
        {hint && <span className="text-xs text-slate-400">{hint}</span>}
      </div>
      <div>{children}</div>
    </div>
  );
}

function BalanceCard({ detail }: { detail: MerchantDetail }) {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
      <Tile label="Available" value={paiseToRupees(detail.balance.available_paise)} accent="text-slate-900" />
      <Tile label="Held (in flight)" value={paiseToRupees(detail.balance.held_paise)} accent="text-amber-700" />
      <Tile label="Total credited" value={paiseToRupees(detail.balance.total_credited_paise)} accent="text-emerald-700" />
    </div>
  );
}

function Tile({ label, value, accent }: { label: string; value: string; accent: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white shadow-sm p-4">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`text-2xl font-semibold mt-1 ${accent} tabular-nums`}>{value}</div>
    </div>
  );
}

function PayoutForm({
  merchantId,
  bankAccounts,
  availablePaise,
  onResult,
}: {
  merchantId: string;
  bankAccounts: BankAccount[];
  availablePaise: number;
  onResult: (t: Toast) => void;
}) {
  const activeAccounts = useMemo(() => bankAccounts.filter((b) => b.is_active), [bankAccounts]);
  const [bankAccountId, setBankAccountId] = useState<string>(activeAccounts[0]?.id ?? "");
  const [amountRupees, setAmountRupees] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (activeAccounts.length && !activeAccounts.find((a) => a.id === bankAccountId)) {
      setBankAccountId(activeAccounts[0].id);
    }
  }, [activeAccounts, bankAccountId]);

  const amountPaise = Math.round(Number(amountRupees) * 100);
  const validAmount = Number.isFinite(amountPaise) && amountPaise > 0;
  const exceeds = validAmount && amountPaise > availablePaise;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!validAmount || !bankAccountId || submitting) return;
    setSubmitting(true);
    const idempotencyKey = crypto.randomUUID();
    try {
      const payout = await api.createPayout({
        merchantId,
        bankAccountId,
        amountPaise,
        idempotencyKey,
      });
      onResult({
        kind: "success",
        text: `Payout ${payout.id.slice(0, 8)}… created (${paiseToRupees(payout.amount_paise)})`,
      });
      setAmountRupees("");
    } catch (e) {
      onResult({ kind: "error", text: (e as Error).message || "Payout failed" });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Section title="Request payout">
      <form onSubmit={submit} className="p-4 space-y-3">
        <label className="block">
          <span className="text-xs text-slate-600">Bank account</span>
          <select
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            value={bankAccountId}
            onChange={(e) => setBankAccountId(e.target.value)}
          >
            {activeAccounts.map((b) => (
              <option key={b.id} value={b.id}>
                {b.account_holder_name} · {b.account_number_masked} · {b.ifsc}
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="text-xs text-slate-600">Amount (₹)</span>
          <input
            type="number"
            inputMode="decimal"
            step="0.01"
            min="0.01"
            value={amountRupees}
            onChange={(e) => setAmountRupees(e.target.value)}
            placeholder="100.00"
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm tabular-nums"
          />
          <span className={`mt-1 block text-xs ${exceeds ? "text-rose-600" : "text-slate-500"}`}>
            Available: {paiseToRupees(availablePaise)}{exceeds ? " — request exceeds available" : ""}
          </span>
        </label>
        <button
          type="submit"
          disabled={!validAmount || !bankAccountId || submitting}
          className="w-full rounded-md bg-slate-900 text-white text-sm font-medium py-2 px-4 hover:bg-slate-800 disabled:bg-slate-300 disabled:cursor-not-allowed"
        >
          {submitting ? "Submitting…" : "Request payout"}
        </button>
        <p className="text-[11px] text-slate-400">
          A new <code className="text-slate-500">Idempotency-Key</code> is generated per submission.
        </p>
      </form>
    </Section>
  );
}

function StatusPill({ status }: { status: Payout["status"] }) {
  const styles: Record<Payout["status"], string> = {
    PENDING: "bg-slate-100 text-slate-700",
    PROCESSING: "bg-amber-100 text-amber-800",
    COMPLETED: "bg-emerald-100 text-emerald-800",
    FAILED: "bg-rose-100 text-rose-800",
  };
  return (
    <span className={`inline-flex rounded-full px-2 py-0.5 text-[11px] font-medium ${styles[status]}`}>
      {status}
    </span>
  );
}

function PayoutTable({ rows }: { rows: Payout[] }) {
  if (rows.length === 0) return <Empty>No payouts yet.</Empty>;
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full text-sm">
        <thead className="bg-slate-50 text-slate-600">
          <tr>
            <Th>Time</Th>
            <Th>Amount</Th>
            <Th>Status</Th>
            <Th>Attempts</Th>
            <Th>ID</Th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {rows.map((p) => (
            <tr key={p.id}>
              <Td>{new Date(p.created_at).toLocaleString()}</Td>
              <Td className="tabular-nums">{paiseToRupees(p.amount_paise)}</Td>
              <Td><StatusPill status={p.status} /></Td>
              <Td className="text-center">{p.attempts}</Td>
              <Td className="font-mono text-xs text-slate-500">{p.id.slice(0, 8)}…</Td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LedgerTable({ rows }: { rows: LedgerEntry[] }) {
  if (rows.length === 0) return <Empty>No ledger entries yet.</Empty>;
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full text-sm">
        <thead className="bg-slate-50 text-slate-600">
          <tr>
            <Th>Time</Th>
            <Th>Type</Th>
            <Th>Status</Th>
            <Th>Amount</Th>
            <Th>Description</Th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {rows.map((e) => (
            <tr key={e.id}>
              <Td>{new Date(e.created_at).toLocaleString()}</Td>
              <Td>
                <span className={e.entry_type === "CREDIT" ? "text-emerald-700 font-medium" : "text-slate-700"}>
                  {e.entry_type === "CREDIT" ? "+ CREDIT" : "− DEBIT"}
                </span>
              </Td>
              <Td>
                <span className={`text-xs ${e.status === "HOLD" ? "text-amber-700" : "text-slate-500"}`}>{e.status}</span>
              </Td>
              <Td className="tabular-nums">{paiseToRupees(e.amount_paise)}</Td>
              <Td className="text-slate-600 truncate max-w-xs">{e.description}</Td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return <th className="text-left px-4 py-2 text-xs font-medium uppercase tracking-wide">{children}</th>;
}

function Td({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <td className={`px-4 py-2 ${className}`}>{children}</td>;
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="px-4 py-6 text-center text-sm text-slate-400">{children}</div>;
}
