<script lang="ts">
	// The settings page shell: the runtime settings as a draft, grouped into
	// cards, saved in one go. The accounts linked with other services live here
	// too — settings that are not a field but a session with another service.

	import { t, type MessageKey } from '$lib/i18n';
	import {
		Button,
		Card,
		SaveBar,
		SettingField,
		SettingsDraft,
		formatBytes,
		request,
		toasts
	} from '$lib/kit';
	import { install } from '$lib/core/install.svelte';
	import LinkedAccount from '$lib/core/LinkedAccount.svelte';
	import type { Link } from '$lib/core/types';
	import ChannelOrder from '$lib/music/settings/ChannelOrder.svelte';

	// the order the cards are read in: what the whole install shares first, then
	// each half. The token other modules call with is the account page's, not a
	// field among these.
	const SHARED = ['acquire'];
	const MUSIC = ['music_library', 'music_acquire', 'music_metadata'];
	const VIDEO = ['video_library', 'video_acquire', 'video_subtitles', 'video_metadata'];
	const PHOTOS = ['photos_library', 'photos_people', 'photos_contacts'];

	const form = new SettingsDraft();
	let links = $state<Link[]>([]);
	let services = $state<{ value: string; label: string }[] | undefined>(undefined);
	type LandingPreview = {
		keep_days: number;
		candidates: number;
		protected: number;
		stale: number;
		reclaimable: number;
		entries: { path: string; idle_days: number; bytes: number }[];
		truncated: boolean;
	};
	let landing = $state<LandingPreview | null>(null);
	let previewingLanding = $state(false);

	// photographs are a type an install may simply not have, and then neither
	// are the settings that tune how its faces are found
	let order = $derived([...SHARED, ...MUSIC, ...VIDEO, ...(install.types.includes('photos') ? PHOTOS : [])]);
	let groups = $derived(
		order.map((name) => ({ name, items: form.of(name) })).filter((g) => g.items.length > 0)
	);

	async function loadLinks() {
		links = (await request<Link[]>('/api/links')) ?? links;
	}

	$effect(() => {
		form.load();
		install.load();
		loadLinks();
	});

	$effect(() => {
		if (!form.settings.some((s) => s.key === 'streaming_subscriptions')) return;
		void request<{ id: number; name: string }[]>('/api/video/discover/streaming').then(
			(found) => (services = found?.map((s) => ({ value: String(s.id), label: s.name })))
		);
	});

	async function previewLanding() {
		previewingLanding = true;
		try {
			landing = await request<LandingPreview>('/api/landing');
		} finally {
			previewingLanding = false;
		}
	}
</script>

<form
	onsubmit={(e) => {
		e.preventDefault();
		form.save();
	}}
>
	<div class="cards">
		{#each groups as group (group.name)}
			<Card title={t(`settings.group.${group.name}` as MessageKey)} collapsible>
				<div class="fields">
					{#each group.items as s (s.key)}
						{#if s.key === 'channel_order'}
							<div class="wide"><ChannelOrder bind:value={form.draft[s.key]} channels={s.options ?? []} /></div>
						{:else if s.key === 'streaming_subscriptions'}
							<div class="wide">
								<SettingField setting={s} bind:value={form.draft[s.key]} choices={services} />
							</div>
						{:else}
							<SettingField setting={s} bind:value={form.draft[s.key]} />
						{/if}
					{/each}
				</div>
			</Card>
		{/each}

		{#each links as link, i (link.name)}
			<LinkedAccount {link} onchanged={(now) => (links[i] = now)} />
		{/each}

		{#if form.settings.some((s) => s.key === 'landing_keep_days')}
			<Card title={t('settings.landing.title')} collapsible>
				<p class="landing-copy">{t('settings.landing.copy')}</p>
				<Button onclick={previewLanding} disabled={previewingLanding}>
					{previewingLanding ? t('settings.landing.checking') : t('settings.landing.check')}
				</Button>
				{#if landing}
					<p class="landing-summary">
						{t('settings.landing.summary', {
							stale: landing.stale,
							bytes: formatBytes(landing.reclaimable),
							keep: landing.keep_days,
							protected: landing.protected
						})}
					</p>
					{#if landing.entries.length}
						<ul class="landing-entries">
							{#each landing.entries as entry (entry.path)}
								<li><code>{entry.path}</code><span>{formatBytes(entry.bytes)} · {entry.idle_days} d</span></li>
							{/each}
						</ul>
						{#if landing.truncated}<p>{t('settings.landing.truncated')}</p>{/if}
					{:else}
						<p>{t('settings.landing.none')}</p>
					{/if}
				{/if}
			</Card>
		{/if}
	</div>

	<SaveBar dirty={form.dirty} saving={form.saving} />
</form>

<style>
	.cards {
		display: grid;
		gap: 1.25rem;
	}
	.landing-copy, .landing-summary {
		margin: 0 0 0.75rem;
	}
	.landing-entries {
		list-style: none;
		margin: 0.75rem 0 0;
		padding: 0;
	}
	.landing-entries li {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
		padding: 0.4rem 0;
		border-bottom: 1px solid var(--border);
	}
	.landing-entries code {
		overflow-wrap: anywhere;
	}
	.landing-entries span {
		white-space: nowrap;
		color: var(--muted);
	}
</style>
