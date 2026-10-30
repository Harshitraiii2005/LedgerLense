import { DatePipe, JsonPipe } from '@angular/common';
import { Component, OnInit, inject, signal } from '@angular/core';

import { ApiError, ApiService } from '../core/api';
import { AuditEntry } from '../core/models';
import { ErrorBox } from './shared';

@Component({
  selector: 'app-audit',
  imports: [DatePipe, JsonPipe, ErrorBox],
  template: `
    <h1>Audit log</h1>
    <app-error [error]="error()" />
    <div class="table-wrap">
      <table>
        <thead>
          <tr><th>Time</th><th>User</th><th>Role</th><th>Question</th><th>Allowed</th><th>Pipeline</th></tr>
        </thead>
        <tbody>
          @for (e of entries(); track e._id) {
            <tr [class.flagged]="!e.allowed">
              <td>{{ e.timestamp | date: 'short' }}</td>
              <td>{{ e.user }}</td>
              <td>{{ e.role }}</td>
              <td>{{ e.question }}</td>
              <td>{{ e.allowed ? 'yes' : 'rejected: ' + (e.reason ?? '') }}</td>
              <td><details><summary>view</summary><pre>{{ e.pipeline | json }}</pre></details></td>
            </tr>
          } @empty {
            <tr><td colspan="6" class="muted">{{ loaded() ? 'No agent queries yet.' : 'Loading…' }}</td></tr>
          }
        </tbody>
      </table>
    </div>
  `,
})
export class AuditPage implements OnInit {
  private readonly api = inject(ApiService);
  readonly entries = signal<AuditEntry[]>([]);
  readonly loaded = signal(false);
  readonly error = signal<ApiError | null>(null); // a 403 from the gateway lands here too

  ngOnInit(): void {
    this.api.audit().subscribe({
      next: (r) => {
        this.entries.set(r.data);
        this.loaded.set(true);
      },
      error: (e: ApiError) => {
        this.error.set(e);
        this.loaded.set(true);
      },
    });
  }
}
