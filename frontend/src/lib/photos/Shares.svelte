<script lang="ts">
	// What has been handed outside the house, and until when. The link itself
	// cannot be shown again; what can be done with one already out is to
	// withdraw it.

	import { ArmedButton, formatDate, plural, request, toasts } from '$lib/kit';
	import { tileOf } from '$lib/opus';
	import { t } from '$lib/i18n';

	type Share = {
		id: number;
		made_by: string;
		made_at: string;
		expires_at: string;
		photos: number;
		cover: { id: string; turn: number } | null;
	};

	let shares = $state<Share[] | null>(null);
	$effect(() => {
		request<Share[]>('/api/photos/shares').then((said) => (shares = said ?? []));
	});

	async function revoke(id: number) {
		if (!(await request(`/api/photos/shares/${id}`, { method: 'DELETE' }))) return;
		shares = (shares ?? []).filter((s) => s.id !== id);
		toasts.success(t('photos.shared.revoked'));
	}

	const now = Date.now();
</script>

{#if shares?.length === 0}
	<p class="empty">{t('photos.shared.none')}</p>
{:else if shares}
	<ul class="shares">
		{#each shares as s (s.id)}
			{@const out = Date.parse(s.expires_at) > now}
			<li class:past={!out}>
				{#if s.cover}
					<img src={tileOf(s.cover.id, s.cover.turn)} alt="" loading="lazy" decoding="async" />
				{:else}
					<span class="none"></span>
				{/if}
				<div class="about">
					<strong>{plural(s.photos, 'photos.count.one', 'photos.count.few', 'photos.count.many')}</strong>
					<span>{s.made_by} · {formatDate(s.made_at)}</span>
					<span class="when">
						{t(out ? 'photos.shared.until' : 'photos.shared.expired', { date: formatDate(s.expires_at) })}
					</span>
				</div>
				<ArmedButton size="small" onconfirm={() => revoke(s.id)}>{t('photos.shared.revoke')}</ArmedButton>
			</li>
		{/each}
	</ul>
{/if}

<style>
	.empty {
		color: var(--muted);
	}
	.shares {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		gap: 0.6rem;
	}
	li {
		display: flex;
		align-items: center;
		gap: 0.9rem;
		padding: 0.5rem;
		border: 1px solid var(--border);
		border-radius: var(--radius-m);
		background: var(--surface);
	}
	li.past {
		opacity: 0.6;
	}
	img,
	.none {
		width: 4rem;
		height: 4rem;
		flex: none;
		object-fit: cover;
		border-radius: var(--radius-s);
		background: var(--surface-2);
	}
	.about {
		flex: 1;
		min-width: 0;
		display: grid;
		gap: 0.15rem;
		font-size: var(--fs-m);
	}
	.about span {
		color: var(--muted);
		font-size: var(--fs-s);
	}
</style>
