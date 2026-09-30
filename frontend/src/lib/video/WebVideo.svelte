<script lang="ts">
	import { t } from '$lib/i18n';
	import {
		ArmedButton,
		Button,
		Heading,
		SearchBox,
		Stats,
		Tag,
		duration,
		json,
		request,
		toasts,
		type Stat
	} from '$lib/kit';
	import {
		videoStateMark,
		StateMark
	} from '$lib/opus';

	type Channel = { id: number; url: string; title: string; monitored: boolean };
	type Video = {
		id: number;
		url: string;
		title: string;
		uploader: string;
		thumb_url: string | null;
		duration_s: number | null;
		status: 'wanted' | 'downloading' | 'complete';
	};

	let url = $state('');
	let busy = $state(false);
	let channels = $state<Channel[]>([]);
	let videos = $state<Video[]>([]);
	let loaded = $state(false);

	// counted here rather than fetched: the page already holds every row it
	// would count, and a second endpoint would only be able to disagree
	let stats = $derived<Stat[]>([
		{ label: t('stats.channels'), value: channels.length },
		{ label: t('stats.videos'), value: videos.length },
		{
			label: t('stats.complete'),
			value: videos.filter((v) => v.status === 'complete').length,
			tone: 'ok'
		},
		{
			label: t('stats.wanted'),
			value: videos.filter((v) => v.status === 'wanted').length,
			tone: 'warn'
		}
	]);

	async function load() {
		const got = await request<{ channels: Channel[]; videos: Video[] }>('/api/video/videos');
		if (!got) return;
		channels = got.channels;
		videos = got.videos;
		loaded = true;
	}

	$effect(() => {
		load();
	});

	async function add() {
		if (!url.trim()) return;
		busy = true;
		const said = await request<{ kind: string; title: string }>('/api/video/videos', json({ url: url.trim() }), {
			on: { 409: () => toasts.info(t('webvideo.duplicate')) }
		});
		busy = false;
		if (!said) return;
		toasts.success(
			said.kind === 'channel'
				? t('webvideo.channelFollowed', { title: said.title })
				: t('webvideo.videoAdded', { title: said.title })
		);
		url = '';
		await load();
	}

	async function download(video: Video) {
		if (!(await request(`/api/video/videos/${video.id}/download`, { method: 'POST' }))) return;
		toasts.success(t('webvideo.downloadStarted'));
		await load();
	}

	async function removeChannel(channel: Channel) {
		if (!(await request(`/api/video/videos/channels/${channel.id}`, { method: 'DELETE' }))) return;
		toasts.success(t('webvideo.channelRemoved'));
		await load();
	}
</script>

<Stats {stats} />

<div class="add">
	<SearchBox
		url
		bind:value={url}
		placeholder={t('webvideo.placeholder')}
		action={busy ? t('common.saving') : t('action.add')}
		tone="accent"
		{busy}
		onsubmit={add}
	/>
</div>

{#if channels.length > 0}
	<Heading label={t('webvideo.followedChannels')} />
	<ul class="channels">
		{#each channels as channel (channel.id)}
			<li>
				<strong>{channel.title}</strong>
				<span class="muted url">{channel.url}</span>
				<ArmedButton onconfirm={() => removeChannel(channel)}>{t('common.delete')}</ArmedButton>
			</li>
		{/each}
	</ul>
{/if}

<Heading label={t('webvideo.videos')} />
{#if loaded && videos.length === 0}
	<p class="muted">{t('webvideo.empty')}</p>
{/if}
<ul class="videos">
	{#each videos as video (video.id)}
		<li>
			{#if video.thumb_url}
				<img src={video.thumb_url} alt={video.title} loading="lazy" />
			{:else}
				<div class="placeholder"></div>
			{/if}
			<div class="info">
				<strong>{video.title}</strong>
				<span class="muted">
					{[video.uploader, video.duration_s ? duration(video.duration_s) : ''].filter(Boolean).join(' · ')}
				</span>
			</div>
			{#if videoStateMark(video.status)}<StateMark {...videoStateMark(video.status)!} text={t(`video.status.${video.status}`)} />{/if}
			{#if video.status === 'wanted'}
				<Button size="small" tone="accent" onclick={() => download(video)}>{t('webvideo.download')}</Button>
			{/if}
		</li>
	{/each}
</ul>

<style>
	.add {
		margin: 1.25rem 0;
	}
	.channels,
	.videos {
		list-style: none;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	.channels li,
	.videos li {
		display: flex;
		align-items: center;
		gap: 0.9rem;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 10px;
		padding: 0.5rem 1rem;
	}
	.channels strong {
		min-width: 12em;
	}
	.url {
		flex: 1;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.videos img,
	.placeholder {
		width: 96px;
		height: 54px;
		border-radius: 6px;
		object-fit: cover;
		background: var(--surface-2);
	}
	.info {
		flex: 1;
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
		min-width: 0;
		font-size: var(--fs-m);
	}
	.info strong {
		font-size: var(--fs-l);
	}
</style>
