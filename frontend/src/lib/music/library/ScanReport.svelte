<script lang="ts">
	// The per-folder verdict of the last import scan, newest folder first, with
	// the toggle that hides everything that went cleanly. An adopted or partial
	// folder shows its matched/total count as a link into the artist it landed
	// on; anything else shows why it did not land.

	import { t, type MessageKey } from '$lib/i18n';
	import { Button, Tag, formatNumber, json, request, toasts } from '$lib/kit';
	import FolderReport from './FolderReport.svelte';
	import type { ScanReason, ScanResult, ScanState } from '$lib/music/types';

	let {
		scan,
		onretry
	}: { scan: ScanState | null; onretry?: () => Promise<void> } = $props();

	let problems = $derived(scan ? scan.results.filter((r) => r.outcome !== 'adopted').length : 0);
	let problemsOnly = $state(false);
	let reportRows = $derived(
		scan
			? [...scan.results].reverse().filter((r) => !problemsOnly || r.outcome !== 'adopted')
			: []
	);

	function outcomeText(entry: ScanResult): string {
		return t(`libimport.outcome.${entry.outcome}` as MessageKey, {
			matched: formatNumber(entry.matched ?? 0),
			total: formatNumber(entry.total ?? 0)
		});
	}

	const REASONS: Record<ScanReason, MessageKey> = {
		missing_tags: 'libimport.reason.missing_tags',
		files_unreadable: 'libimport.reason.files_unreadable',
		artist_unresolved: 'libimport.reason.artist_unresolved',
		catalog_unreachable: 'libimport.reason.catalog_unreachable',
		match_error: 'libimport.reason.match_error',
		cached_verdict: 'libimport.reason.cached_verdict',
		already_complete: 'libimport.reason.already_complete',
		no_catalog_candidate: 'libimport.reason.no_catalog_candidate',
		all_tracks_matched: 'libimport.reason.all_tracks_matched',
		edition_confirmed: 'libimport.reason.edition_confirmed',
		partial_track_match: 'libimport.reason.partial_track_match'
	};

	let retrying = $state<string | null>(null);

	async function retry(entry: ScanResult) {
		retrying = entry.folder;
		try {
			const said = await request<ScanResult>(
				'/api/music/library/folders/rematch',
				json({ folder: entry.folder })
			);
			if (!said) return;
			toasts.info(`${entry.folder}: ${outcomeText(said)}`);
			await onretry?.();
		} finally {
			retrying = null;
		}
	}
</script>

{#if scan && scan.results.length > 0}
	<FolderReport
		summary={`${t('libimport.report')} (${formatNumber(scan.results.length)}/${formatNumber(scan.total)})`}
		open={scan.running}
	>
		{#snippet above()}
			<label class="only">
				<input type="checkbox" bind:checked={problemsOnly} />
				{t('libimport.problemsOnly', { count: formatNumber(problems) })}
			</label>
		{/snippet}
		{#each reportRows as entry (entry.folder)}
			<tr>
				<td class="folder"><code>{entry.folder}</code></td>
				<td>
					{#if entry.artist}{entry.artist}{#if entry.album} — {entry.album}{/if}{/if}
				</td>
				<td>
					{#if (entry.outcome === 'adopted' || entry.outcome === 'partial') && entry.artist_id}
						<a href={`/artists/${entry.artist_id}`}>
							<Tag tone={entry.outcome === 'adopted' ? 'ok' : 'warn'}>
								{formatNumber(entry.matched ?? 0)}/{formatNumber(entry.total ?? 0)}
							</Tag>
						</a>
					{:else}
						<span class:failed={entry.outcome !== 'adopted' && entry.outcome !== 'partial'}
							class:partial={entry.outcome === 'partial'}>{outcomeText(entry)}</span
						>
						{#if entry.detail}<span class="detail">{entry.detail}</span>{/if}
						{#if entry.reason}<span class="reason">{t(REASONS[entry.reason])}</span>{/if}
						{#if entry.outcome === 'catalog_unavailable'}
							<Button size="small" disabled={scan.running || retrying === entry.folder} onclick={() => retry(entry)}>
								{t('music.library.rematch')}
							</Button>
						{/if}
					{/if}
				</td>
			</tr>
		{/each}
	</FolderReport>
{/if}

<style>
	.only {
		display: inline-flex;
		align-items: center;
		gap: 0.4rem;
		margin: 0.5rem 0;
		font-size: var(--fs-m);
		color: var(--muted);
		cursor: pointer;
	}
	.detail {
		display: block;
		margin-top: 0.2rem;
		color: var(--muted);
		font-family: var(--mono, monospace);
		font-size: 0.8em;
	}
	.reason {
		display: block;
		margin-top: 0.2rem;
		color: var(--muted);
		font-size: 0.8em;
	}
	.detail + :global(.btn) {
		margin-top: 0.35rem;
	}
</style>
