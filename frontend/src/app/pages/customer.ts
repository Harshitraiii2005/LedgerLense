import { DatePipe, JsonPipe, KeyValuePipe } from '@angular/common';
import { Component, effect, inject, input, signal } from '@angular/core';

import { ApiError, ApiService } from '../core/api';
import { CustomerV2 } from '../core/models';
import { ErrorBox } from './shared';

@Component({
  selector: 'app-customer',
  imports: [DatePipe, JsonPipe, KeyValuePipe, ErrorBox],
  template: `
    <h1>Customer {{ id() }}</h1>
    <app-error [error]="error()" />
    @if (customer(); as c) {
      <section class="card">
        <h2>Risk profile</h2>
        @if (c.risk_profile; as p) {
          <dl class="kv">
            @for (kv of p | keyvalue; track kv.key) {
              <dt>{{ kv.key }}</dt>
              <dd>{{ kv.value | json }}</dd>
            }
          </dl>
        } @else if (c.risk_profile_error; as e) {
          <!-- Degraded mode: risk service down, Mongo data still shown below -->
          <p class="warn">Risk profile unavailable ({{ e.code }}): {{ e.message }}</p>
        }
      </section>

      <section class="card">
        <h2>Recent transactions</h2>
        <div class="table-wrap">
          <table>
            <thead>
              <tr><th>Time</th><th>Merchant</th><th>Category</th><th>Amount</th><th>Country</th><th>Risk</th></tr>
            </thead>
            <tbody>
              @for (t of c.recent_transactions; track t.txn_id) {
                <tr [class.flagged]="t.risk?.flagged">
                  <td>{{ t.timestamp | date: 'short' }}</td>
                  <td>{{ t.merchant }}</td>
                  <td>{{ t.category }}</td>
                  <td class="money">{{ t.money.amount }} {{ t.money.currency }}</td>
                  <td>{{ t.country }}</td>
                  <td>{{ t.risk ? t.risk.score : 'not scored' }}</td>
                </tr>
              }
            </tbody>
          </table>
        </div>
      </section>
    } @else if (!error()) {
      <p class="muted">Loading…</p>
    }
  `,
})
export class CustomerPage {
  private readonly api = inject(ApiService);
  readonly id = input.required<string>(); // bound from the :id route param
  readonly customer = signal<CustomerV2 | null>(null);
  readonly error = signal<ApiError | null>(null);

  constructor() {
    effect(() => {
      const id = this.id();
      this.customer.set(null);
      this.error.set(null);
      this.api.customer(id).subscribe({
        next: (c) => this.customer.set(c),
        error: (e: ApiError) => this.error.set(e),
      });
    });
  }
}
