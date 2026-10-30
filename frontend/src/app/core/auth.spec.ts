import { TestBed } from '@angular/core/testing';
import { ActivatedRouteSnapshot, Router, RouterStateSnapshot, UrlTree, provideRouter } from '@angular/router';

import { AuthService, adminGuard } from './auth';

/** Unsigned JWT with the given claims: the client only decodes, the gateway verifies. */
function fakeJwt(role: string, expInS = 3600): string {
  const b64 = (o: object) => btoa(JSON.stringify(o)).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_');
  return `${b64({ alg: 'HS256' })}.${b64({ sub: 'u1', role, exp: Date.now() / 1000 + expInS })}.sig`;
}

function runGuard() {
  return TestBed.runInInjectionContext(() =>
    adminGuard({} as ActivatedRouteSnapshot, {} as RouterStateSnapshot),
  );
}

describe('adminGuard', () => {
  beforeEach(() => TestBed.configureTestingModule({ providers: [provideRouter([])] }));
  afterEach(() => sessionStorage.clear());

  const urlOf = (r: unknown) => TestBed.inject(Router).serializeUrl(r as UrlTree);

  it('lets admins through', () => {
    TestBed.inject(AuthService).login(fakeJwt('admin'));
    expect(runGuard()).toBe(true);
  });

  it('sends analysts to /forbidden', () => {
    TestBed.inject(AuthService).login(fakeJwt('analyst'));
    expect(urlOf(runGuard())).toBe('/forbidden');
  });

  it('sends anonymous or expired sessions to /login', () => {
    expect(urlOf(runGuard())).toBe('/login');
    TestBed.inject(AuthService).login(fakeJwt('admin', -10));
    expect(urlOf(runGuard())).toBe('/login');
  });
});
