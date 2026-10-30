import { HttpClient, HttpErrorResponse, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, catchError, throwError } from 'rxjs';

import {
  AgentAnswer,
  ApiErrorBody,
  AuditEntry,
  CustomerV2,
  PageV2,
  Role,
  TxnFilter,
} from './models';

/** Everything goes through /api: the dev proxy and nginx both forward it to the gateway. */
export const API_BASE = '/api';

/** Normalised error every page renders: the gateway's {code, message, request_id} plus HTTP status. */
export interface ApiError extends ApiErrorBody {
  status: number;
}

export function toApiError(err: unknown): ApiError {
  if (err instanceof HttpErrorResponse) {
    const body = err.error as Partial<ApiErrorBody> | null;
    if (body && typeof body === 'object' && typeof body.code === 'string') {
      return {
        status: err.status,
        code: body.code,
        message: body.message ?? '',
        request_id: body.request_id ?? err.headers.get('X-Request-ID'),
      };
    }
    // No gateway body: the proxy couldn't reach the gateway, or the network is down.
    return {
      status: err.status,
      code: err.status === 0 ? 'network_error' : 'http_error',
      message: err.message,
      request_id: err.headers?.get('X-Request-ID') ?? null,
    };
  }
  return { status: 0, code: 'client_error', message: String(err), request_id: null };
}

@Injectable({ providedIn: 'root' })
export class ApiService {
  private readonly http = inject(HttpClient);

  private call<T>(obs: Observable<T>): Observable<T> {
    return obs.pipe(catchError((e: unknown) => throwError(() => toApiError(e))));
  }

  devToken(sub: string, role: Role): Observable<{ access_token: string }> {
    return this.call(this.http.post<{ access_token: string }>(`${API_BASE}/dev/token`, { sub, role }));
  }

  transactions(filter: TxnFilter, cursor: string | null, limit = 20): Observable<PageV2> {
    let params = new HttpParams().set('limit', limit);
    for (const [k, v] of Object.entries(filter)) {
      if (v !== undefined && v !== '') params = params.set(k, String(v));
    }
    if (cursor) params = params.set('cursor', cursor);
    return this.call(this.http.get<PageV2>(`${API_BASE}/v2/transactions`, { params }));
  }

  customer(id: string): Observable<CustomerV2> {
    return this.call(this.http.get<CustomerV2>(`${API_BASE}/v2/customers/${encodeURIComponent(id)}`));
  }

  askAgent(question: string): Observable<AgentAnswer> {
    return this.call(this.http.post<AgentAnswer>(`${API_BASE}/v2/agent/query`, { question }));
  }

  audit(limit = 50): Observable<{ data: AuditEntry[] }> {
    return this.call(this.http.get<{ data: AuditEntry[] }>(`${API_BASE}/v2/audit`, { params: { limit } }));
  }
}
