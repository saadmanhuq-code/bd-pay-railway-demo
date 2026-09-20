// Bulk disbursement CSV parsing + per-row validation (maker side).
// Expected header: beneficiary_name,account_number,routing_number,amount_bdt,reference
// Amounts use string/BigInt math via parseAmountToMinor (no floats); Bengali
// numerals are normalized client-side as a convenience — the server re-runs
// normalization and is authoritative.

import type { CsvRowErrorCode, CsvRowReport, DisbursementRow } from "@/lib/api/types";
import { normalizeBengaliDigits, parseAmountToMinor } from "@/lib/format";

export const CSV_HEADER = "beneficiary_name,account_number,routing_number,amount_bdt,reference";

export interface CsvParseResult {
  reports: CsvRowReport[];
  validRows: DisbursementRow[];
  totalMinor: string;
  headerOk: boolean;
}

function splitCsvLine(line: string): string[] {
  // Simple CSV: comma-separated, optional double quotes around a field.
  const out: string[] = [];
  let cur = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (inQuotes) {
      if (ch === '"') {
        if (line[i + 1] === '"') {
          cur += '"';
          i++;
        } else {
          inQuotes = false;
        }
      } else {
        cur += ch;
      }
    } else if (ch === '"') {
      inQuotes = true;
    } else if (ch === ",") {
      out.push(cur);
      cur = "";
    } else {
      cur += ch;
    }
  }
  out.push(cur);
  return out.map((s) => s.trim());
}

export function parseDisbursementCsv(text: string): CsvParseResult {
  const lines = text.split(/\r?\n/).filter((l) => l.trim() !== "");
  const headerLine = lines[0] ?? "";
  const headerOk = headerLine.replace(/\s/g, "").toLowerCase() === CSV_HEADER.replace(/\s/g, "");
  const reports: CsvRowReport[] = [];
  const validRows: DisbursementRow[] = [];
  const seenRefs = new Set<string>();
  let total = 0n;

  const dataLines = headerOk ? lines.slice(1) : lines;
  dataLines.forEach((line, idx) => {
    const rowNo = idx + 1;
    const cells = splitCsvLine(line);
    const errors: CsvRowErrorCode[] = [];
    const name = cells[0] ?? "";
    const account = normalizeBengaliDigits(cells[1] ?? "");
    const routing = normalizeBengaliDigits(cells[2] ?? "");
    const amountRaw = cells[3] ?? "";
    const reference = cells[4] ?? "";

    if (name === "" || account === "" || routing === "" || amountRaw === "" || reference === "") {
      errors.push("missing_field");
    }
    const amountMinor = parseAmountToMinor(amountRaw);
    if (amountRaw !== "" && amountMinor === null) {
      errors.push("bad_amount");
    }
    if (amountMinor !== null && BigInt(amountMinor) <= 0n) {
      errors.push("amount_not_positive");
    }
    if (account !== "" && !/^\d{8,20}$/.test(account)) {
      errors.push("bad_account");
    }
    // Bangladesh bank routing numbers are 9 digits.
    if (routing !== "" && !/^\d{9}$/.test(routing)) {
      errors.push("bad_routing");
    }
    if (reference !== "") {
      if (seenRefs.has(reference)) {
        errors.push("duplicate_reference");
      }
      seenRefs.add(reference);
    }

    if (errors.length === 0 && amountMinor !== null) {
      const row: DisbursementRow = {
        row_no: rowNo,
        beneficiary_name: name,
        account_number: account,
        routing_number: routing,
        amount_minor: amountMinor,
        reference,
      };
      validRows.push(row);
      total += BigInt(amountMinor);
      reports.push({ row_no: rowNo, row, errors: [] });
    } else {
      reports.push({ row_no: rowNo, row: null, errors });
    }
  });

  return { reports, validRows, totalMinor: total.toString(), headerOk };
}
