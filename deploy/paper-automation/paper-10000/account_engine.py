"""One pool of paper cash. Entry proof, price-level fees and official redemption use the installed project."""
from collections import Counter
from decimal import Decimal as D, ROUND_DOWN
from polymarket861 import budget_archive_proof as proof, five_league_budget as budget, ledger_rules as r, store

FUND=D('10000')
FRACTION=D('0.25')
POLICY='shared-quarter-kelly-v1'
START=FUND
fmt=r.fmt

def size_at_book(asks,fee,model_fair,comparison_fair,capital,limit):
    """Largest gross size whose actual debit is within the quarter-Kelly budget at its own net cost."""
    effective_p=(r.unit(model_fair)+r.unit(comparison_fair))/2
    allowed=[(p,q,p,p) for p,q in asks if p<=limit]
    if not allowed:return None
    quantum=D('0.01')
    upper=min(sum((q for _,q,_,_ in allowed),D(0)),capital/allowed[0][0])
    lo,hi=0,int(upper//quantum)
    while lo<hi:
        mid=(lo+hi+1)//2
        trial=r.price_fill(allowed,D(mid)*quantum,fee)
        c=trial['cost']
        target=capital*FRACTION*max(D(0),(effective_p-c)/(1-c)) if 0<c<1 else D(0)
        if trial['spent']>0 and trial['spent']<=target:lo=mid
        else:hi=mid-1
    if not lo:return None
    fill=r.price_fill(allowed,D(lo)*quantum,fee)
    c=fill['cost']
    target=capital*FRACTION*(effective_p-c)/(1-c)
    return {'shares':fill['gross'],'budget':target,'planning_cost':c,'planning_spend':fill['spent'],'probability':effective_p}

def replay(document, *, as_of=None, data_dir=None, details=True, entry_basis='scheduled_with_incomplete_flags'):
    if document.get('evidence_mode')!='local_receipts' or document.get('schema')!=budget.SCHEMA or document.get('account_policy')!=POLICY or entry_basis!='scheduled_with_incomplete_flags':
        raise ValueError('shared paper account requires authenticated local receipts and the recorded portfolio policy')
    stop=r.instant(as_of or store.now_utc())
    by_id,remaining=budget._books(document['books'])
    originals=budget._indexed(document['books'],'books')
    intents=budget._indexed(document['intents'],'intents')
    redemptions=budget._indexed(document['redemptions'],'redemptions')
    events=[]
    used_tokens=set()
    for key,op in intents.items():
        if op['token_id'] in used_tokens:raise ValueError('duplicate held token')
        used_tokens.add(op['token_id'])
        seen=[by_id[op[k]] for k in ('decision_book','execution_book')]
        ident=(op['fixture_id'],op['condition_id'],op['token_id'])
        if any(b[:3]!=ident for b in seen) or not seen[0][3]<=r.instant(op['decision_at'])<seen[1][3]<r.instant(op['kickoff']):
            raise ValueError('intent and books or kickoff disagree')
        events.append((seen[1][3],1,op['rank'],key,op))
    for key,event in redemptions.items():
        if event['token_id'] not in used_tokens:raise ValueError('payment references unknown position')
        events.append((r.instant(event['at']),0,0,key,event))
    cash=FUND
    leagues={k:{'spent':D(0),'returned':D(0),'redeemed_cost':D(0),'orders':0,'closed':0} for k in budget.CODES}
    decisions=[];positions={};paid={};counts=Counter();gaps=Counter()
    with proof.read_archive(data_dir) as source,proof.football_archive(source) as football:
        for when,kind,rank,key,op in sorted(events):
            if when>stop:break
            league=budget._scope(op['fixture_id']); group=leagues[league]; token=op['token_id']
            if kind==0:
                held=positions.get(token)
                if not held:continue
                if op['fixture_id']!=held['op']['fixture_id'] or op['condition_id']!=held['op']['condition_id'] or when<r.instant(held['op']['kickoff']):
                    raise ValueError('redemption scoped to another match or condition')
                payout,gap=proof.redemption(source,held['market'],op,r.instant(held['op']['kickoff']))
                if gap:
                    gaps[gap]+=1
                    continue
                if token in paid:
                    if paid[token][0]!=payout:raise ValueError('conflicting payout')
                    continue
                amount=(held['net']*payout).quantize(r.UNIT,rounding=ROUND_DOWN)
                cash+=amount;group['returned']+=amount;group['redeemed_cost']+=held['spent'];group['closed']+=1
                paid[token]=(payout,op);counts['positions_redeemed']+=1
                continue
            first,second=[originals[op[k]] for k in ('decision_book','execution_book')]
            decision={'id':key,'at':r.iso(when),'league':league,'fixture_id':op['fixture_id'],'outcome':'skipped_unverified_entry','cost_including_cash_fees':None,'fee_evidence':None}
            try:
                m=proof.entry(source,op,(first,second),scheduled=True,as_of=stop,football=football)
            except (ValueError,TypeError,KeyError,OSError) as exc:
                gaps[str(exc)]+=1
                decision['reason']=str(exc)
                decisions.append(decision)
                continue
            qty=r.number(op['shares']);limit=r.unit(op['limit_price']);fee=m['entry_terms']['fee_schedule']
            if qty<=0 or qty%r.UNIT or limit<=0:raise ValueError('invalid requested size or limit')
            plan=op['sizing'];fair=r.unit(plan['model_fair']);ext=r.unit(plan['external_fair']);p=(fair+ext)/2
            if r.unit(plan['kelly_fraction'])!=FRACTION or not D(0)<r.number(plan['sizing_bankroll'])<=FUND:
                raise ValueError('wrong risk policy')
            source_keys=[by_id[op[k]][4] for k in ('decision_book','execution_book')]
            levels=[sorted((a,b) for a,b in remaining[src].items() if b>0) for src in source_keys]
            fill=r.price_fill(r.pair_depth(*levels,limit),qty,fee)
            good_quote=True
            for book in (first,second):
                ask=r.number(book['asks'][0]['price']);bid=r.number(book['bids'][0]['price'])
                good_quote &= 0 < ask-bid <= r.MAX_SPREAD and abs(fair-(ask+bid)/2)<=r.MAX_GAP and abs(r.unit(plan['news_fair'])-(ask+bid)/2)<=r.MAX_GAP
            c=fill['cost']
            target=(r.number(plan['sizing_bankroll'])*FRACTION*max(D(0),(p-c)/(1-c))) if 0<c<1 else D(0)
            if fill['gross']!=qty or not fill['net']:decision['outcome']='skipped_depth'
            elif not good_quote or fair-c<r.MIN_EDGE or p<=c:decision['outcome']='skipped_price'
            elif fill['spent']>target or fill['spent']>r.number(plan['budget']):decision['outcome']='skipped_kelly_budget'
            elif fill['spent']>cash:decision['outcome']='skipped_cash'
            else:
                for a,b,used in fill['used']:
                    remaining[source_keys[0]][a]-=used
                    remaining[source_keys[1]][b]-=used
                cash-=fill['spent'];group['spent']+=fill['spent'];group['orders']+=1
                positions[token]={'market':m,'op':op,'net':fill['net'],'spent':fill['spent']}
                decision.update(outcome='filled',cost_including_cash_fees=fmt(fill['spent']),fee_evidence=m['fee_evidence'],entry_evidence=m['entry_evidence'],fill={'spent':fmt(fill['spent']),'gross_shares':fmt(fill['gross']),'net_shares':fmt(fill['net']),'cost_per_net_share':fmt(c),'levels':fill['levels'],'fee_cash_equivalent':fmt(fill['fees'])})
            decisions.append(decision)
        for token,(amount,event) in paid.items():
            pos=positions[token];proof.audit_redemption(source,pos['market'],event,r.instant(pos['op']['kickoff']),stop,amount)
    total_open=sum((p['spent'] for k,p in positions.items() if k not in paid),D(0))
    realized=sum((z['returned']-z['redeemed_cost'] for z in leagues.values()),D(0))
    if cash<0 or cash+total_open!=FUND+realized:raise ValueError('shared cash conservation failed')
    summaries={league:{'buy_cash_including_fees':fmt(z['spent']),'unredeemed_cost':fmt(z['spent']-z['redeemed_cost']),'cash_from_redemptions':fmt(z['returned']),'settled_paper_pnl':fmt(z['returned']-z['redeemed_cost']),'filled':z['orders'],'redeemed':z['closed']} for league,z in leagues.items()}
    return {'schema':budget.SCHEMA,'account_policy':POLICY,'as_of':r.iso(stop),'evidence_mode':'local_receipts','verified_orders_at_venue':0,'opening_cash':fmt(FUND),'limits':{'initial_cash':fmt(FUND),'kelly_fraction':fmt(FRACTION)},'leagues':summaries,'total_available_cash':fmt(cash),'total_unredeemed_cost':fmt(total_open),'total_settled_paper_pnl':fmt(realized),'redemption_events':dict(counts),'proof_gaps':dict(gaps),'decisions':decisions if details else [],'venue_realized_pnl':None}
