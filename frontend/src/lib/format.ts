// Display formatting. Amounts stay strings end to end: no float ever touches a total.

const DECIMAL = /^(-?)(\d+)(\.\d+)?$/;

export function formatMoney(amount: string | null, currency: string | null): string {
  if (amount === null) return "—";
  const match = DECIMAL.exec(amount.trim());
  const suffix = currency ? ` ${currency}` : "";
  if (!match) return `${amount}${suffix}`;
  const [, sign, whole, fraction = ""] = match;
  return `${sign}${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}${fraction}${suffix}`;
}

const DATE_TIME = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  timeZone: "UTC",
});

export function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return `${DATE_TIME.format(date)} UTC`;
}

export function shortHash(hash: string | null): string {
  return hash ? hash.slice(0, 8) : "";
}

export function paragraphs(text: string): string[] {
  return text
    .split(/\n\s*\n/)
    .map((p) => p.trim())
    .filter((p) => p.length > 0);
}
