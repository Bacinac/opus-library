<script lang="ts">
	// Every candidate any enabled channel finds for one album, unfiltered —
	// nothing dropped for a zero match, a ceiling, or a missing DSD word in its
	// title, because the reason this is open at all is that the automatic grab
	// already tried and none of that was good enough on its own. Grabbing here
	// is stateless: the row picked round-trips its own channel/title/ref back
	// to /grab, the same shape the video picker already uses.

	import { t } from '$lib/i18n';
	import { Button, Dialog, Tag, formatBytes, formatNumber, json, plural, request, toasts } from '$lib/kit';
	import { offeredQuality } from '$lib/music/quality';
	import type { OfferedQuality } from '$lib/music/types';

	type Candidate = {
		channel: string;
		title: string;
		ref: Record<string, unknown>;
		score: number;
		completeness: number;
		multi_album: boolean;
		whole_album: boolean;
		size: number | null;
		quality: OfferedQuality;
	};
	type Mode = 'full' | 'replace' | 'surround' | 'dsd';

	let {
		releaseId,
		title,
		onclose,
		ongrabbed
	}: { releaseId: number; title: string; onclose: () => void; ongrabbed: () => void } = $props();

	let candidates = $state<Candidate[]>([]);
	let loading = $state(true);
	let failed = $state(false);
	let grabbing = $state(false);
	let mode = $state<Mode>('full');

	async function load() {
		loading = true;
		failed = false;
		const got = await request<{ candidates: Candidate[] }>(
			`/api/music/releases/${releaseId}/candidates`
		);
		loading = false;
		if (got) candidates = got.candidates;
		else failed = true;
	}

	$effect(() => {
		load();
	});

	async function grab(c: Candidate) {
		if (grabbing) return;
		grabbing = true;
		const started = await request(
			`/api/music/releases/${releaseId}/grab`,
			json({ channel: c.channel, title: c.title, ref: c.ref, mode, multi_album: c.multi_album })
		);
		grabbing = false;
		if (!started) return;
		toasts.success(t('musicPicker.grabStarted', { title: c.title }));
		ongrabbed();
		onclose();
	}
</script>

<Dialog title={t('musicPicker.heading')} subtitle={title} size="wide" {onclose}>
	{#snippet actions()}
		<label class="mode">
			{t('musicPicker.mode')}
			<select bind:value={mode}>
				<option value="full">{t('musicPicker.modeFull')}</option>
				<option value="replace">{t('musicPicker.modeReplace')}</option>
				<option value="surround">{t('musicPicker.modeSurround')}</option>
				<option value="dsd">{t('musicPicker.modeDsd')}</option>
			</select>
		</label>
	{/snippet}
	{#if loading}
		<p class="note">{t('musicPicker.loading')}</p>
	{:else if failed}
		<p class="note failed">{t('musicPicker.searchError')}</p>
	{:else if candidates.length === 0}
		<p class="note">{t('musicPicker.empty')}</p>
	{:else}
		<p class="count">
			{plural(candidates.length, 'musicPicker.count.one', 'musicPicker.count.few', 'musicPicker.count.many')}
		</p>
		<ul>
			{#each candidates as c, i (c.channel + c.title + i)}
				<li>
					<button class="cand" onclick={() => grab(c)} disabled={grabbing}>
						<Tag>{offeredQuality(c.quality)}</Tag>
						<span class="main">
							<span class="ctitle">{c.title}</span>
							<span class="meta">
								{#if i === 0}<Tag tone="ok">{t('musicPicker.best')}</Tag>{/if}
								<Tag tone="busy">{c.channel}</Tag>
								{#if c.completeness === 0}
									<Tag tone="warn" title={t('musicPicker.unmatchedTitle')}>
										{t('musicPicker.unmatched')}
									</Tag>
								{/if}
								{#if c.multi_album}
									<Tag tone="warn" title={t('musicPicker.multiAlbumTitle')}>
										{t('musicPicker.multiAlbum')}
									</Tag>
								{/if}
								<span class="dim">{c.size ? formatBytes(c.size) : '—'}</span>
							</span>
						</span>
						<span class="score" title={t('musicPicker.score')}>{formatNumber(c.score)}</span>
					</button>
				</li>
			{/each}
		</ul>
	{/if}
</Dialog>

<style>
	.note {
		color: var(--muted);
		padding: 1.5rem 0.5rem;
		text-align: center;
	}
	.note.failed {
		color: var(--danger);
	}
	.count {
		color: var(--muted);
		font-size: var(--fs-s);
		margin: 0 0.5rem 0.6rem;
	}
	.mode {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		font-size: var(--fs-s);
		color: var(--muted);
	}
	ul {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}
	.cand {
		width: 100%;
		display: flex;
		align-items: center;
		gap: 0.85rem;
		text-align: left;
		font: inherit;
		padding: 0.55rem 0.75rem;
		border-radius: 9px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		cursor: pointer;
	}
	.cand:hover:not(:disabled) {
		border-color: var(--accent);
		background: var(--surface-2);
	}
	.cand:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.main {
		flex: 1;
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
		min-width: 0;
	}
	.ctitle {
		font-size: var(--fs-m);
		line-height: 1.3;
		overflow-wrap: anywhere;
	}
	.meta {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.4rem;
		font-size: var(--fs-s);
	}
	.dim {
		color: var(--muted);
	}
	.score {
		flex: none;
		font-size: var(--fs-m);
		font-weight: 700;
		font-variant-numeric: tabular-nums;
		color: var(--muted);
		min-width: 3rem;
		text-align: right;
	}
</style>
