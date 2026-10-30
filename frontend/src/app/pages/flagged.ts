import { DatePipe } from '@angular/common';
import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';

import { ApiError, ApiService } from '../core/api';
import { TxnFilter, TxnV2 } from '../core/models';
import { ErrorBox } from './shared';

type SortKey = 'timestamp' | 'amount' | 'score';

function toIso(local: string): string | undefined {
  return local ? new Date(local).toISOString() : undefined;
}

@Component({
  selector: 'app-flagged',
  imports: [DatePipe, FormsModule, RouterLink, ErrorBox],
  template: `
    <h1>Flagged transactions</h1>
    <form class="filters" (ngSubmit)="applyFilters()">
      <label>Customer <input name="customer" [(ngModel)]="customer" placeholder="C000123" pattern="C\\d{6}" /></label>
      <label>Merchant <input name="merchant" [(ngModel)]="merchant" /></label>
      <label>From <input name="from" type="datetime-local" [(ngModel)]="from" /></label>
      <label>To <input name="to" type="datetime-local" [(ngModel)]="to" /></label>
      <button type="submit">Apply</button>
    </form>

    <app-error [error]="error()" />

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th><button class="link" (click)="sortBy('timestamp')">Time {{ arrow('timestamp') }}</button></th>
            <th>Customer</th>
            <th>Merchant</th>
            <th><button class="link" (click)="sortBy('amount')">Amount {{ arrow('amount') }}</button></th>
            <th><button class="link" (click)="sortBy('score')">Score {{ arrow('score') }}</button></th>
            <th>Reasons</th>
          </tr>
        </thead>
        <tbody>
          @for (t of rows(); track t.txn_id) {
            <tr>
              <td>{{ t.timestamp | date: 'short' }}</td>
              <td><a [routerLink]="['/customers', t.customer_id]">{{ t.customer_id }}</a></td>
              <td>{{ t.merchant }}</td>
              <td class="money">{{ t.money.amount }} {{ t.money.currency }}</td>
              <td>{{ t.risk?.score ?? '–' }}</td>
              <td>
                @for (r of t.risk?.reasons ?? []; track r.code) {
                  <span class="chip" [title]="r.detail">{{ r.code }}</span>
                }
              </td>
            </tr>
          } @empty {
            <tr><td colspan="6" class="muted">{{ loading() ? 'Loading…' : 'No flagged transactions match.' }}</td></tr>
          }
        </tbody>
      </table>
    </div>

    <div class="pager">
      <button (click)="prev()" [disabled]="loading() || cursors().length <= 1">← Prev</button>
      <span class="muted">Page {{ cursors().length }}</span>
      <button (click)="next()" [disabled]="loading() || !nextCursor()">Next →</button>
    </div>
    <p class="muted">Pages are fetched with the v2 cursor; sorting reorders the current page.</p>
  `,
})
export class FlaggedPage implements OnInit {
  private readonly api = inject(ApiService);

  customer = '';
  merchant = '';
  from = '';
  to = '';

  /** cursors[i] is the cursor that fetched page i+1 (null = first page). Prev = pop. */
  readonly cursors = signal<(string | null)[]>([null]);
  readonly nextCursor = signal<string | null>(null);
  readonly page = signal<TxnV2[]>([]);
  readonly loading = signal(false);
  readonly error = signal<ApiError | null>(null);
  readonly sort = signal<{ key: SortKey; desc: boolean }>({ key: 'timestamp', desc: true });

  // Trade-off: client-side sort of the current page only; the gateway always returns newest first.
  readonly rows = computed(() => {
    const { key, desc } = this.sort();
    const value = (t: TxnV2): number =>
      key === 'timestamp' ? Date.parse(t.timestamp) : key === 'amount' ? Number(t.money.amount) : (t.risk?.score ?? 0);
    return [...this.page()].sort((a, b) => (desc ? value(b) - value(a) : value(a) - value(b)));
  });

  private filter: TxnFilter = { flagged: true };

  ngOnInit(): void {
    this.load();
  }

  applyFilters(): void {
    this.filter = {
      flagged: true,
      customer_id: this.customer.trim() || undefined,
      merchant: this.merchant.trim() || undefined,
      from: toIso(this.from),
      to: toIso(this.to),
    };
    this.cursors.set([null]);
    this.load();
  }

  next(): void {
    this.cursors.update((c) => [...c, this.nextCursor()]);
    this.load();
  }

  prev(): void {
    this.cursors.update((c) => c.slice(0, -1));
    this.load();
  }

  sortBy(key: SortKey): void {
    this.sort.update((s) => ({ key, desc: s.key === key ? !s.desc : true }));
  }

  arrow(key: SortKey): string {
    const s = this.sort();
    return s.key === key ? (s.desc ? '▼' : '▲') : '';
  }

  private load(): void {
    const c = this.cursors();
    this.loading.set(true);
    this.error.set(null);
    this.api.transactions(this.filter, c[c.length - 1]).subscribe({
      next: (p) => {
        this.page.set(p.data);
        this.nextCursor.set(p.next_cursor);
        this.loading.set(false);
      },
      error: (e: ApiError) => {
        this.error.set(e);
        this.page.set([]);
        this.loading.set(false);
      },
    });
  }
}
