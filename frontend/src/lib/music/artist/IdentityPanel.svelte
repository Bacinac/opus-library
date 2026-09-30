<script lang="ts">
	// The manual way out when Wikidata could not resolve the artist: search the
	// entity by hand and pin the QID the user picks. An identity is never
	// guessed here — linking only re-queues enrichment, the page reads the
	// result on its next load.

	import { t } from '$lib/i18n';
	import { Button, Latest, Notice, SearchBox, json, request, toasts } from '$lib/kit';
	import type { Candidate } from '$lib/music/types';

	let { artistId }: { artistId: number } = $props();

	let identityQuery = $state('');
	let candidates = $state<Candidate[]>([]);
	let searchingIdentity = $state(false);
	const asking = new Latest();

	async function searchIdentity() {
		if (!identityQuery.trim()) return;
		searchingIdentity = true;
		const found = await request<Candidate[]>(
			`/api/music/artists/${artistId}/identity-candidates?q=${encodeURIComponent(identityQuery)}`,
			{},
			{ latest: asking }
		);
		searchingIdentity = false;
		if (found) candidates = found;
	}

	async function linkIdentity(candidate: Candidate) {
		const queued = await request(
			`/api/music/artists/${artistId}/identity`,
			json({ qid: candidate.qid }, 'PUT')
		);
		if (!queued) return;
		toasts.info(t('artist.identityQueued'));
		candidates = [];
	}
</script>

<Notice tone="warn">
	<p>{t('artist.unresolved')}</p>
	<SearchBox
		bind:value={identityQuery}
		placeholder={t('artist.identityPlaceholder')}
		action={t('search.button')}
		busy={searchingIdentity}
		onsubmit={searchIdentity}
	/>
	{#if candidates.length > 0}
		<ul class="candidates">
			{#each candidates as candidate (candidate.qid)}
				<li>
					<div class="info">
						<strong>{candidate.label}</strong>
						<span class="muted">{candidate.description}</span>
					</div>
					<Button tone="accent" onclick={() => linkIdentity(candidate)}>{t('artist.identitySet')}</Button>
				</li>
			{/each}
		</ul>
	{/if}
</Notice>

<style>
	.candidates {
		list-style: none;
		margin: 0.25rem 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}
	.candidates li {
		display: flex;
		align-items: center;
		gap: 1rem;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 0.45rem 0.8rem;
	}
	.info {
		display: flex;
		flex-direction: column;
		flex: 1;
	}
</style>
