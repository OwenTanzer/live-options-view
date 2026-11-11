// backend/paper-trade.js (B4) -- paper trading: the /api/paper-trade route,
// the largest single function in the Worker (304 lines). Moved verbatim
// from worker.js; the router in worker.js still owns the route table. It
// imports the execution fencing (backend/execution.js), the live-quote fetch
// (backend/browser-data.js), and the session/account helpers
// (backend/common.js).

import {
  MAX_BODY_BYTES,
  checkOrigin, computeBookFromTrades, jsonResponse, readLimited,
  requireSession, withUserRecord,
} from './common.js';
import { fetchLiveQuotes } from './browser-data.js';
import { operationalBucket } from './operational-storage.js';
import {
  exactExecutionQuote, executionIntentMatches, executionPriceForOrder,
  executionReservationExpired, existingExecutionResponse,
  finalizeExecutionRequest, quoteProviderErrorResponse,
  renewExecutionReservation, reserveExecutionRequest, validateTradeIntent,
} from './execution.js';

// ---------------------------------------------------------------------------
// Trade execution
// ---------------------------------------------------------------------------
// Serializes each client-generated request id with a leased R2 `pending`
// reservation. The owner refreshes the reservation immediately before account
// mutation; that conditional ETag write fences off stale owners after a retry
// takes over an abandoned lease. Rejections finalize the same reservation for
// idempotent replay.
//
// Auth is now a real session (see requireSession above), not the shared
// static token this endpoint used to check. The Origin allowlist, a
// Cloudflare rate limiting rule (configured at the zone level, not in this
// file), and the body-size cap remain as defense in depth.
export async function handlePaperTrade(request, env) {
  const session = await requireSession(request, env);
  if (!session) return new Response('Forbidden', { status: 401 });

  const originError = checkOrigin(request);
  if (originError) return originError;

  // Execution records share a bucket with the capture producer. Every read and
  // conditional write below goes through the operational boundary, so a defect
  // in key construction refuses instead of overwriting a published artifact.
  // A missing binding keeps the endpoint's existing 503 rather than surfacing
  // as an unhandled Worker error.
  let executionStore;
  try {
    executionStore = operationalBucket(env, 'paper_trade');
  } catch {
    return jsonResponse({ error: 'Execution state unavailable' }, 503);
  }

  const contentLength = Number(request.headers.get('Content-Length') || 0);
  if (contentLength > MAX_BODY_BYTES) {
    return new Response('Payload too large', { status: 413 });
  }

  let raw;
  try {
    raw = await readLimited(request, MAX_BODY_BYTES);
  } catch (e) {
    return new Response('Payload too large', { status: 413 });
  }

  let body;
  try {
    body = JSON.parse(raw);
  } catch (e) {
    return new Response('Invalid JSON', { status: 400 });
  }
  const validationError = validateTradeIntent(body);
  if (validationError) {
    return new Response(validationError, { status: 400 });
  }

  const executionId = body.execution_request_id;
  const key = `paper-trades/requests/${executionId}.json`;
  let reserved;

  const responseForStoredOutcome = async (storedOutcome, etag) => {
    if (storedOutcome.username !== session.username || !executionIntentMatches(storedOutcome, body)) {
      return jsonResponse({ error: 'execution_request_id conflicts with a different trade intent' }, 409);
    }
    if (storedOutcome.status === 'pending') {
      // Closure and its exact rejected intent are retained together in KV.
      // Recover a crash after closure but before R2 outcome finalization,
      // without obtaining a new quote or attempting another account mutation.
      const closure = session.record.closure_execution;
      if (session.record.account_closed && closure?.execution_request_id === executionId) {
        try {
          const recovered = await finalizeExecutionRequest(executionStore, key, closure, etag);
          return existingExecutionResponse(recovered.outcome, body);
        } catch (error) {
          return jsonResponse({ error: 'Closed-account rejection could not be recovered' }, 503);
        }
      }
      const appliedTrade = session.record.trades.find(t => t.execution_request_id === executionId);
      if (!appliedTrade) {
        if (session.record.account_closed) {
          try {
            const recovered = await finalizeExecutionRequest(executionStore, key,
              { ...storedOutcome, status: 'rejected', http_status: 403,
                error: 'Account closed', account_closed: true }, etag);
            return existingExecutionResponse(recovered.outcome, body);
          } catch (error) {
            return jsonResponse({ error: 'Closed-account pending intent could not be recovered' }, 503);
          }
        }
        return jsonResponse({
          error: 'Execution is already pending',
          order_id: executionId,
          status: 'pending',
        }, 409);
      }
      try {
        const recovered = await finalizeExecutionRequest(executionStore, key, appliedTrade, etag);
        storedOutcome = recovered.outcome;
      } catch (error) {
        return jsonResponse({ error: 'Pending execution could not be recovered' }, 503);
      }
    }
    if (storedOutcome.status === 'rejected') return existingExecutionResponse(storedOutcome, body);
    if (storedOutcome.status !== 'filled' || !Number.isFinite(storedOutcome.price)) {
      return jsonResponse({ error: 'Execution state is invalid' }, 503);
    }
    return jsonResponse({ ...storedOutcome, balance_cash: session.record.balance_cash }, 200);
  };

  try {
    const existing = await executionStore.get(key);
    if (existing) {
      const storedOutcome = await existing.json();
      const appliedTrade = session.record.trades.find(t => t.execution_request_id === executionId);
      if (
        storedOutcome.username !== session.username ||
        !executionIntentMatches(storedOutcome, body) ||
        storedOutcome.status !== 'pending' ||
        session.record.account_closed ||
        appliedTrade ||
        !executionReservationExpired(storedOutcome)
      ) {
        return responseForStoredOutcome(storedOutcome, existing.etag);
      }

      const renewed = await renewExecutionReservation(
        executionStore,
        key,
        storedOutcome,
        existing.etag,
        new Date().toISOString(),
      );
      if (!renewed.updated) return responseForStoredOutcome(renewed.outcome, renewed.etag);
      reserved = renewed;
    }
  } catch (error) {
    return jsonResponse({ error: 'Execution state unavailable' }, 503);
  }

  if (session.record.account_closed) {
    return jsonResponse({ error: 'Account closed', account_closed: true,
      closure_reason: session.record.closure_reason }, 403);
  }

  let quotePayload;
  try {
    const quoteResponse = await fetchLiveQuotes([body.sym], env);
    if (!quoteResponse.ok) return quoteProviderErrorResponse(quoteResponse);
    quotePayload = await quoteResponse.json();
  } catch (error) {
    return jsonResponse({ error: 'Fresh quote unavailable' }, 503);
  }
  const quoteReceivedAt = new Date();

  const quoteResult = exactExecutionQuote(quotePayload, body, quoteReceivedAt.getTime());
  if (quoteResult.error) {
    return jsonResponse({ error: quoteResult.error }, quoteResult.status);
  }

  const executedAt = new Date();
  const priceResult = executionPriceForOrder(quoteResult, body);
  const common = {
    execution_id: executionId,
    execution_request_id: executionId,
    ts: executedAt.toISOString(),
    quote_received_ts: quoteReceivedAt.toISOString(),
    quote_ts: quoteResult.quoteTs,
    bid_ts: quoteResult.bidTs,
    ask_ts: quoteResult.askTs,
    sym: body.sym,
    underlying: /^([A-Z]+)\d{6}[CP]\d{8}$/.exec(body.sym)?.[1] ?? body.sym,
    instrument_type: body.instrument_type ?? 'option',
    multiplier: quoteResult.multiplier,
    strike: quoteResult.strike,
    type: quoteResult.type,
    exp: quoteResult.exp,
    side: body.side,
    qty: body.qty,
    order_id: executionId,
    order_type: body.order_type ?? 'market',
    limit_price: body.order_type === 'limit' ? body.limit_price : null,
    bid: quoteResult.bid,
    ask: quoteResult.ask,
    username: session.username,
  };
  const candidate = Object.freeze(priceResult.error
    ? { ...common, error: priceResult.error, status: 'rejected', http_status: 409 }
    : { ...common, status: 'filled', price: priceResult.price });
  if (!reserved) {
    const reservation = Object.freeze({
      execution_request_id: executionId,
      sym: body.sym,
      instrument_type: body.instrument_type ?? 'option',
      side: body.side,
      qty: body.qty,
      order_type: body.order_type ?? 'market',
      limit_price: body.order_type === 'limit' ? body.limit_price : null,
      username: session.username,
      status: 'pending',
      ts: executedAt.toISOString(),
    });
    try {
      reserved = await reserveExecutionRequest(executionStore, key, reservation);
    } catch (error) {
      return jsonResponse({ error: 'Execution could not be reserved' }, 503);
    }
    if (!reserved.created) return responseForStoredOutcome(reserved.outcome, reserved.etag);
  }

  if (candidate.status === 'rejected') {
    try {
      const finalized = await finalizeExecutionRequest(executionStore, key, candidate, reserved.etag);
      return responseForStoredOutcome(finalized.outcome, finalized.etag);
    } catch (error) {
      return jsonResponse({ error: 'Execution rejection could not be recorded' }, 503);
    }
  }

  let trade = candidate;
  // Buys debit cash and are capped by balance below. Option sells retain the
  // existing uncovered-short behavior; share sells are constrained to the
  // held long quantity inside the account mutation below.
  const multiplier = trade.multiplier ?? 100;
  let financingCost = 0;
  let financingDays = 0;
  let cashDelta = trade.side === 'buy'
    ? -(trade.price * trade.qty * multiplier)
    : (trade.price * trade.qty * multiplier);
  // Financing is charged only when closing a leveraged share position. The
  // loan is the negative cash created by the entry; one annual rate and the
  // elapsed calendar days are enough for this single-position paper bot.
  if (trade.side === 'sell' && (trade.instrument_type ?? 'option') === 'share') {
    const record = session.record;
    const rate = Number(record.paper_financing_rate ?? 0);
    const loan = Math.max(0, -Number(record.balance_cash ?? 0));
    const entry = (record.trades || []).find(t =>
      t.side === 'buy' && (t.instrument_type ?? 'option') === 'share' && t.sym === trade.sym);
    if (rate > 0 && loan > 0 && entry?.ts) {
      const entryMs = Date.parse(entry.ts);
      const elapsed = Number.isFinite(entryMs) ? executedAt.getTime() - entryMs : 0;
      financingDays = Math.max(0, elapsed / 86_400_000);
      financingCost = Math.round(loan * rate * financingDays / 365 * 100) / 100;
      cashDelta -= financingCost;
      trade = { ...trade, financing_cost: financingCost,
        financing_days: Number(financingDays.toFixed(4)),
        financing_rate_annual: rate };
    }
  }

  try {
    const fenced = await renewExecutionReservation(
      executionStore,
      key,
      reserved.outcome,
      reserved.etag,
      new Date().toISOString(),
    );
    if (!fenced.updated) return responseForStoredOutcome(fenced.outcome, fenced.etag);
    reserved = fenced;
  } catch (error) {
    return jsonResponse({ error: 'Execution lease could not be refreshed' }, 503);
  }

  const kvOutcome = await withUserRecord(env, session.username, (record) => {
    const existingTrade = record.trades.find(t => t.execution_request_id === executionId);
    if (existingTrade) {
      return { result: { trade: existingTrade, balance_cash: record.balance_cash } };
    }
    if (record.account_closed) return { error: 'account_closed' };
    if (trade.side === 'buy' && (record.balance_cash < 0 || -cashDelta > record.balance_cash)) {
      const marginLimit = Number(record.paper_margin_limit ?? 1);
      const book = computeBookFromTrades(record.trades);
      const isFlat = Object.values(book).every(position => position.pos === 0);
      const leveragedEntry = record.is_bot === true &&
        (trade.instrument_type ?? 'option') === 'share' &&
        Number.isFinite(marginLimit) && marginLimit > 1 && marginLimit <= 4 && isFlat;
      if (!leveragedEntry || -cashDelta > record.balance_cash * marginLimit) {
        if (record.is_bot) {
          const rejection = { ...common, error: 'Insufficient balance', status: 'rejected',
            http_status: 400, account_closed: true };
          return { record: { ...record, account_closed: true, closed_at: executedAt.toISOString(),
            closure_reason: 'insufficient_balance', closure_execution: rejection },
            result: { balance_rejection: rejection } };
        }
        return { error: 'insufficient_balance' };
      }
    }
    if ((trade.instrument_type ?? 'option') === 'share' && trade.side === 'sell') {
      const held = computeBookFromTrades(record.trades)[trade.sym]?.pos ?? 0;
      if (held < trade.qty) return { error: 'insufficient_position' };
    }
    const balance_cash = record.balance_cash + cashDelta;
    return {
      record: { ...record, balance_cash, trades: [...record.trades, trade] },
      result: { trade, balance_cash },
    };
  });

  if (kvOutcome.error === 'insufficient_balance' || kvOutcome.result?.balance_rejection) {
    const rejection = Object.freeze(kvOutcome.result?.balance_rejection ?? {
      ...common,
      error: 'Insufficient balance',
      status: 'rejected',
      http_status: 400,
    });
    try {
      const finalized = await finalizeExecutionRequest(executionStore, key, rejection, reserved.etag);
      return responseForStoredOutcome(finalized.outcome, finalized.etag);
    } catch (error) {
      return jsonResponse({ error: 'Balance rejection could not be recorded' }, 503);
    }
  }
  if (kvOutcome.error === 'account_closed') {
    const rejection = { ...common, error: 'Account closed', account_closed: true,
      status: 'rejected', http_status: 403 };
    try {
      const finalized = await finalizeExecutionRequest(executionStore, key, rejection, reserved.etag);
      return responseForStoredOutcome(finalized.outcome, finalized.etag);
    } catch (error) {
      return jsonResponse({ error: 'Account closure rejection could not be recorded' }, 503);
    }
  }
  if (kvOutcome.error === 'insufficient_position') {
    const rejection = Object.freeze({
      ...common,
      error: 'Cannot sell more shares than are held',
      status: 'rejected',
      http_status: 400,
    });
    try {
      const finalized = await finalizeExecutionRequest(executionStore, key, rejection, reserved.etag);
      return responseForStoredOutcome(finalized.outcome, finalized.etag);
    } catch (error) {
      return jsonResponse({ error: 'Position rejection could not be recorded' }, 503);
    }
  }
  if (kvOutcome.error) {
    return jsonResponse({ error: 'Balance could not be updated, try again' }, 503);
  }

  try {
    const finalized = await finalizeExecutionRequest(executionStore, key, trade, reserved.etag);
    if (finalized.outcome.status !== 'filled') {
      return jsonResponse({ error: 'Execution finalization conflicted' }, 503);
    }
  } catch (error) {
    return jsonResponse({ error: 'Execution could not be finalized' }, 503);
  }

  return jsonResponse({ ...trade, balance_cash: kvOutcome.result.balance_cash }, 201);
}
