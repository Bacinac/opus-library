<script lang="ts">
	// What the two long-running library jobs are doing right now: the enrich
	// pass and the import scan, each a notice with its own bar. The scan notice
	// also carries the per-artist and deep-match phases, which run inside the
	// same scan and would otherwise look stalled.

	import { t, type MessageKey } from '$lib/i18n';
	import { Notice, Progress, formatNumber } from '$lib/kit';
	import type { EnrichState, ScanPhase, ScanState } from '$lib/music/types';

	let {
		scan,
		enrich,
		scanProgressAt
	}: { scan: ScanState | null; enrich: EnrichState | null; scanProgressAt: number } = $props();

	const STALLED_AFTER_MS = 5 * 60 * 1000;
	// `scan` is replaced at every poll, so this is reconsidered even while the
	// server state itself has stopped moving. The timestamp changes only after
	// the caller observed genuine work.
	let scanStalled = $derived(
		Boolean(scan?.running && scanProgressAt && Date.now() - scanProgressAt > STALLED_AFTER_MS)
	);

	const PASS: Record<ScanPhase, MessageKey> = {
		read: 'libimport.read',
		artists: 'libimport.artists',
		match: 'libimport.match'
	};

	const share = (done: number, of: number) => (of ? done / of : 0);
</script>

{#if enrich?.running}
	<Notice>
		<p>
			{t('libenrich.running', {
				processed: formatNumber(enrich.processed),
				total: formatNumber(enrich.total)
			})}
		</p>
		<Progress value={share(enrich.processed, enrich.total)} />
		{#if enrich.current}
			<p class="muted current">{enrich.current}</p>
		{/if}
	</Notice>
{/if}

{#if scan?.running}
	<Notice>
		<p>
			{scan.phase
				? t(PASS[scan.phase], {
						processed: formatNumber(scan.processed),
						total: formatNumber(scan.total)
					})
				: t('libimport.starting')}
		</p>
		<Progress value={share(scan.processed, scan.total)} />
		{#if scan.current}
			<p class="muted current">{t('libimport.current', { name: scan.current })}</p>
		{/if}
		{#if scan.deep_total > 0}
			<p class="muted current">
				{t('libimport.deep', {
					processed: formatNumber(scan.deep_processed),
					total: formatNumber(scan.deep_total)
				})}
				{#if scan.deep_current}— {scan.deep_current}{/if}
			</p>
		{/if}
		{#if scanStalled}
			<p class="stalled">{t('libimport.stalled')}</p>
		{/if}
	</Notice>
{/if}

<style>
	.current {
		font-size: var(--fs-m);
	}
	.stalled {
		margin: 0.55rem 0 0;
		color: var(--warn);
		font-size: var(--fs-m);
	}
</style>
