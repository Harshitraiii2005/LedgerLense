import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';

import { ApiService } from '../core/api';
import { PageV2, TxnFilter, TxnV2 } from '../core/models';
import { FlaggedPage } from './flagged';

function txn(id: string, amount: string, score: number): TxnV2 {
  return {
    txn_id: id,
    customer_id: 'C000001',
    merchant: 'Acme',
    category: 'travel',
    money: { amount, currency: 'USD' },
    country: 'US',
    channel: 'online',
    timestamp: '2026-10-01T10:00:00Z',
    risk: { score, flagged: true, reasons: [{ code: 'VELOCITY', detail: 'x' }], scored_at: '2026-10-01T10:00:01Z' },
  };
}

describe('FlaggedPage', () => {
  const calls: { filter: TxnFilter; cursor: string | null }[] = [];
  const pages: Record<string, PageV2> = {
    first: { data: [txn('t1', '10.00', 40), txn('t2', '250.00', 90)], next_cursor: 'c2' },
    c2: { data: [txn('t3', '5.00', 70)], next_cursor: null },
  };

  beforeEach(async () => {
    calls.length = 0;
    const api = {
      transactions: (filter: TxnFilter, cursor: string | null) => {
        calls.push({ filter, cursor });
        return of(pages[cursor ?? 'first']);
      },
    };
    await TestBed.configureTestingModule({
      imports: [FlaggedPage],
      providers: [provideRouter([]), { provide: ApiService, useValue: api }],
    }).compileComponents();
  });

  it('pages forward and back with the cursor stack, always filtering flagged', async () => {
    const fixture = TestBed.createComponent(FlaggedPage);
    await fixture.whenStable();
    const page = fixture.componentInstance;
    expect(calls[0]).toEqual({ filter: { flagged: true }, cursor: null });

    page.next();
    expect(calls.at(-1)?.cursor).toBe('c2');
    expect(page.rows().map((t) => t.txn_id)).toEqual(['t3']);
    expect(page.nextCursor()).toBeNull();

    page.prev();
    expect(calls.at(-1)?.cursor).toBeNull();
    expect(page.cursors()).toEqual([null]);
  });

  it('sorts the current page and renders reason chips', async () => {
    const fixture = TestBed.createComponent(FlaggedPage);
    await fixture.whenStable();
    const page = fixture.componentInstance;
    page.sortBy('amount'); // descending first
    expect(page.rows().map((t) => t.txn_id)).toEqual(['t2', 't1']);
    page.sortBy('amount');
    expect(page.rows().map((t) => t.txn_id)).toEqual(['t1', 't2']);

    await fixture.whenStable();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelectorAll('tbody tr').length).toBe(2);
    expect(el.querySelector('.chip')?.textContent).toContain('VELOCITY');
  });

  it('resets to the first page when filters change', async () => {
    const fixture = TestBed.createComponent(FlaggedPage);
    await fixture.whenStable();
    const page = fixture.componentInstance;
    page.next();
    page.customer = ' C000042 ';
    page.applyFilters();
    expect(calls.at(-1)).toEqual({
      filter: { flagged: true, customer_id: 'C000042', merchant: undefined, from: undefined, to: undefined },
      cursor: null,
    });
  });
});
