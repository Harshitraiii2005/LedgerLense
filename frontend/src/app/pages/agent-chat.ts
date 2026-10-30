import { JsonPipe } from '@angular/common';
import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { ApiError, ApiService } from '../core/api';
import { AgentAnswer } from '../core/models';
import { ErrorBox } from './shared';

interface Turn {
  question: string;
  answer?: AgentAnswer;
  error?: ApiError;
}

export function columnsOf(rows: Record<string, unknown>[]): string[] {
  return [...new Set(rows.flatMap((r) => Object.keys(r)))];
}

@Component({
  selector: 'app-agent-chat',
  imports: [FormsModule, JsonPipe, ErrorBox],
  template: `
    <h1>Ask the data</h1>
    <p class="muted">Questions are answered from the business-term catalog; every query is audited.</p>

    @for (t of turns(); track $index) {
      <div class="bubble q">{{ t.question }}</div>
      @if (t.answer; as a) {
        <div class="bubble a card">
          <p>{{ a.answer }}</p>
          @if (a.terms_used.length) {
            <div>Terms: @for (term of a.terms_used; track term) { <span class="chip">{{ term }}</span> }</div>
          }
          <details>
            <summary>Generated pipeline</summary>
            <pre>{{ a.pipeline | json }}</pre>
          </details>
          @if (a.rows.length) {
            <div class="table-wrap">
              <table>
                <thead><tr>@for (col of columns(a); track col) { <th>{{ col }}</th> }</tr></thead>
                <tbody>
                  @for (row of a.rows; track $index) {
                    <tr>@for (col of columns(a); track col) { <td>{{ row[col] | json }}</td> }</tr>
                  }
                </tbody>
              </table>
            </div>
          }
        </div>
      } @else if (t.error) {
        <div class="bubble a"><app-error [error]="t.error" /></div>
      } @else {
        <div class="bubble a muted">Thinking…</div>
      }
    }

    <form class="ask" (ngSubmit)="ask()">
      <input name="q" [(ngModel)]="question" placeholder="e.g. What was the flagged rate per merchant last week?" />
      <button type="submit" [disabled]="busy() || question.trim().length < 3">Ask</button>
    </form>
  `,
})
export class AgentChatPage {
  private readonly api = inject(ApiService);
  question = '';
  readonly turns = signal<Turn[]>([]);
  readonly busy = signal(false);
  protected readonly columns = (a: AgentAnswer) => columnsOf(a.rows);

  ask(): void {
    const question = this.question.trim();
    const i = this.turns().length;
    this.turns.update((t) => [...t, { question }]);
    this.question = '';
    this.busy.set(true);
    const settle = (patch: Partial<Turn>) => {
      this.turns.update((t) => t.map((turn, j) => (j === i ? { ...turn, ...patch } : turn)));
      this.busy.set(false);
    };
    this.api.askAgent(question).subscribe({
      next: (answer) => settle({ answer }),
      error: (error: ApiError) => settle({ error }),
    });
  }
}
