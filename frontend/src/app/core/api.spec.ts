import { HttpErrorResponse, HttpHeaders, provideHttpClient, withInterceptors } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { ApiError, ApiService, toApiError } from './api';
import { AuthService, authInterceptor } from './auth';

function setup() {
  TestBed.configureTestingModule({
    providers: [
      provideRouter([{ path: 'login', children: [] }]),
      provideHttpClient(withInterceptors([authInterceptor])),
      provideHttpClientTesting(),
    ],
  });
  return {
    api: TestBed.inject(ApiService),
    http: TestBed.inject(HttpTestingController),
    auth: TestBed.inject(AuthService),
  };
}

describe('ApiService', () => {
  afterEach(() => sessionStorage.clear());

  it('sends v2 filters + cursor and skips empty values', () => {
    const { api, http } = setup();
    api.transactions({ flagged: true, customer_id: 'C000001', merchant: '' }, 'abc', 20).subscribe();
    const req = http.expectOne((r) => r.url === '/api/v2/transactions');
    expect(req.request.params.get('flagged')).toBe('true');
    expect(req.request.params.get('customer_id')).toBe('C000001');
    expect(req.request.params.has('merchant')).toBe(false);
    expect(req.request.params.get('cursor')).toBe('abc');
    expect(req.request.params.get('limit')).toBe('20');
    req.flush({ data: [], next_cursor: null });
  });

  it('interceptor adds Bearer token and a request id', () => {
    const { api, http, auth } = setup();
    auth.login('tok');
    api.audit().subscribe();
    const req = http.expectOne((r) => r.url === '/api/v2/audit');
    expect(req.request.headers.get('Authorization')).toBe('Bearer tok');
    expect(req.request.headers.get('X-Request-ID')).toBeTruthy();
    req.flush({ data: [] });
  });

  it('surfaces the gateway error schema', () => {
    const { api, http } = setup();
    let err: ApiError | undefined;
    api.askAgent('how many?').subscribe({ error: (e: ApiError) => (err = e) });
    http
      .expectOne('/api/v2/agent/query')
      .flush(
        { code: 'upstream_unavailable', message: 'agent service is unavailable', request_id: 'rid-1' },
        { status: 503, statusText: 'Service Unavailable' },
      );
    expect(err).toEqual({
      status: 503,
      code: 'upstream_unavailable',
      message: 'agent service is unavailable',
      request_id: 'rid-1',
    });
  });

  it('logs out on 401', () => {
    const { api, http, auth } = setup();
    auth.login('expired');
    api.audit().subscribe({ error: () => undefined });
    http.expectOne('/api/v2/audit?limit=50').flush({ code: 'token_expired', message: 'x', request_id: null }, { status: 401, statusText: 'Unauthorized' });
    expect(auth.token()).toBeNull();
  });
});

describe('toApiError', () => {
  it('maps a body-less network failure', () => {
    const e = toApiError(new HttpErrorResponse({ status: 0, headers: new HttpHeaders() }));
    expect(e.code).toBe('network_error');
  });
});
