import { TestScheduler } from 'rxjs/testing';

import { backoffMs, feedUrl, withBackoff } from './live-feed';

describe('backoffMs', () => {
  it('doubles from 1s and caps at 30s', () => {
    expect([1, 2, 3, 4, 5, 6, 7].map((n) => backoffMs(n))).toEqual([1000, 2000, 4000, 8000, 16000, 30000, 30000]);
  });
});

describe('withBackoff', () => {
  const scheduler = () => new TestScheduler((actual, expected) => expect(actual).toEqual(expected));

  it('resubscribes after errors with growing delays', () => {
    const attempts: number[] = [];
    scheduler().run(({ cold, expectObservable, expectSubscriptions }) => {
      const source = cold('#'); // every connection attempt fails immediately
      const result = source.pipe(withBackoff((n) => attempts.push(n)));
      // Attempts at t=0, then +1s, +2s, +4s; the next (+8s) is past the 7.5s window.
      expectObservable(result, '7500ms !').toBe('');
      expectSubscriptions(source.subscriptions).toBe(['(^!)', '1000ms (^!)', '3000ms (^!)', '7000ms (^!)']);
    });
    expect(attempts).toEqual([1, 2, 3, 4]);
  });

  it('reconnects when the server closes the socket, and resets the delay after a message', () => {
    const attempts: number[] = [];
    scheduler().run(({ cold, expectObservable }) => {
      // Each connection delivers one message and then the server closes it.
      const source = cold('a|');
      const result = source.pipe(withBackoff((n) => attempts.push(n)));
      // Because a message arrived, every reconnect waits only the base 1s (resetOnSuccess).
      expectObservable(result, '2500ms !').toBe('a 1000ms a 1000ms a');
    });
    expect(attempts).toEqual([1, 1, 1]); // every retry starts again at attempt 1
  });
});

describe('feedUrl', () => {
  it('uses wss on https and goes through the /api proxy', () => {
    const url = feedUrl('t k', { protocol: 'https:', host: 'app.example' } as Location, null);
    expect(url).toBe('wss://app.example/api/v2/ws/flagged?token=t%20k');
  });

  it('connects straight to the gateway when a ws base is configured', () => {
    const url = feedUrl('t', { protocol: 'https:', host: 'app.example' } as Location, 'wss://gw.example');
    expect(url).toBe('wss://gw.example/v2/ws/flagged?token=t');
  });
});
