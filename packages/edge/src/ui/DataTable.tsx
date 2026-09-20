"use client";

// Generic sortable table. Click a header to sort; click again to reverse.

import React, { useMemo, useState } from "react";

export interface Column<T> {
  key: string;
  label: React.ReactNode;
  sortValue?: (row: T) => string | number;
  render: (row: T) => React.ReactNode;
}

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  onRowClick,
  rowClassName,
  empty,
}: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  onRowClick?: (row: T) => void;
  rowClassName?: (row: T) => string;
  empty?: React.ReactNode;
}) {
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [desc, setDesc] = useState(false);

  const sorted = useMemo(() => {
    if (sortKey === null) return rows;
    const col = columns.find((c) => c.key === sortKey);
    if (!col || !col.sortValue) return rows;
    const sv = col.sortValue;
    const copy = rows.slice().sort((a, b) => {
      const av = sv(a);
      const bv = sv(b);
      if (av < bv) return desc ? 1 : -1;
      if (av > bv) return desc ? -1 : 1;
      return 0;
    });
    return copy;
  }, [rows, columns, sortKey, desc]);

  function onHeader(col: Column<T>) {
    if (!col.sortValue) return;
    if (sortKey === col.key) {
      setDesc((d) => !d);
    } else {
      setSortKey(col.key);
      setDesc(false);
    }
  }

  return (
    <table className="data-table">
      <thead>
        <tr>
          {columns.map((c) => (
            <th key={c.key} onClick={() => onHeader(c)} className={c.sortValue ? "sortable" : ""}>
              {c.label}
              {sortKey === c.key ? <span className="sort-arrow">{desc ? " ▼" : " ▲"}</span> : null}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {sorted.length === 0 ? (
          <tr>
            <td colSpan={columns.length} className="empty-row">
              {empty ?? "No rows"}
            </td>
          </tr>
        ) : (
          sorted.map((row) => (
            <tr
              key={rowKey(row)}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              className={`${onRowClick ? "clickable" : ""} ${rowClassName ? rowClassName(row) : ""}`}
            >
              {columns.map((c) => (
                <td key={c.key}>{c.render(row)}</td>
              ))}
            </tr>
          ))
        )}
      </tbody>
    </table>
  );
}
