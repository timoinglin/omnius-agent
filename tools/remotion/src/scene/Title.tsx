import React from 'react';
import {
	AbsoluteFill,
	Easing,
	interpolate,
	spring,
	useCurrentFrame,
	useVideoConfig,
} from 'remotion';
import {BEAT, CLAMP, decay, since, useSeconds} from './timing';

// System faces only - nothing is fetched at render time, so this renders the
// same on a machine with no network. Impact is the trailer face; the fallbacks
// are the other heavy condensed sans shipped with Windows.
const HEAVY = 'Impact, Haettenschweiler, "Arial Black", "Franklin Gothic Heavy", sans-serif';
const THIN = '"Arial Narrow", "Helvetica Neue", Arial, sans-serif';

const GOLD =
	'linear-gradient(180deg, #fffdf0 0%, #ffeeb0 13%, #ffc93f 29%, #a9760c 48%, #6b4805 51%, #ffd76b 62%, #fff0bc 74%, #c9962a 90%, #7a5510 100%)';

const SILVER =
	'linear-gradient(180deg, #ffffff 0%, #e9edf2 22%, #9aa6b4 48%, #5d6672 52%, #dfe5ec 66%, #ffffff 88%)';

type WordProps = {
	children: string;
	size: number;
	spacing: number;
	fill: string;
	/** chromatic fringe offset, in px */
	aberration: number;
	/** 0..1 position of the specular shimmer, or null for none */
	shimmer: number | null;
};

/**
 * One word of the title, built as a stack:
 *   bevel/depth -> red fringe -> cyan fringe -> metal fill -> shimmer
 * The fringes sit UNDER the fill on purpose: the fill covers their centres and
 * only the offset edges show, which is what chromatic aberration actually looks
 * like. Painted over the top they would just wash the metal out.
 */
const MetalWord: React.FC<WordProps> = ({children, size, spacing, fill, aberration, shimmer}) => {
	const base: React.CSSProperties = {
		fontFamily: HEAVY,
		fontSize: size,
		letterSpacing: spacing,
		lineHeight: 1.06,
		margin: 0,
		whiteSpace: 'nowrap',
		textTransform: 'uppercase',
		fontWeight: 400,
	};
	const layer: React.CSSProperties = {...base, position: 'absolute', left: 0, top: 0};

	return (
		<div style={{position: 'relative', display: 'inline-block'}}>
			{/* depth: extruded edge + drop shadow + glow */}
			<div
				style={{
					...base,
					color: '#191002',
					WebkitTextStroke: `${Math.max(2, size * 0.012)}px #0d0901`,
					textShadow:
						'0 3px 0 #6b4a0a, 0 6px 0 #473106, 0 9px 0 #2a1d03, 0 16px 34px rgba(0,0,0,0.95), 0 0 100px rgba(255,178,58,0.55)',
				}}
			>
				{children}
			</div>

			{aberration > 0.15 ? (
				<>
					<div
						style={{
							...layer,
							color: '#ff1030',
							mixBlendMode: 'screen',
							opacity: 0.85,
							transform: `translateX(${-aberration}px)`,
						}}
					>
						{children}
					</div>
					<div
						style={{
							...layer,
							color: '#12d4ff',
							mixBlendMode: 'screen',
							opacity: 0.85,
							transform: `translateX(${aberration}px)`,
						}}
					>
						{children}
					</div>
				</>
			) : null}

			{/* the metal itself */}
			<div
				style={{
					...layer,
					backgroundImage: fill,
					WebkitBackgroundClip: 'text',
					backgroundClip: 'text',
					color: 'transparent',
				}}
			>
				{children}
			</div>

			{/* specular sweep across the metal */}
			{shimmer === null ? null : (
				<div
					style={{
						...layer,
						backgroundImage:
							'linear-gradient(102deg, transparent 44%, rgba(255,255,255,0.32) 48%, rgba(255,255,255,0.85) 50%, rgba(255,255,255,0.32) 52%, transparent 56%)',
						backgroundSize: '300% 100%',
						backgroundPosition: `${shimmer}% 0`,
						WebkitBackgroundClip: 'text',
						backgroundClip: 'text',
						color: 'transparent',
					}}
				>
					{children}
				</div>
			)}
		</div>
	);
};

/** The setup line, before the joke lands. */
export const LeadLine: React.FC = () => {
	const t = useSeconds();
	if (t < BEAT.leadIn - 0.3 || t > BEAT.leadOut + 0.6) {
		return null;
	}

	const o = interpolate(
		t,
		[BEAT.leadIn, BEAT.leadIn + 0.8, BEAT.leadOut, BEAT.leadOut + 0.45],
		[0, 1, 1, 0],
		CLAMP
	);
	const drift = interpolate(t, [BEAT.leadIn, BEAT.leadOut + 0.5], [1, 1.07], CLAMP);
	const spread = interpolate(t, [BEAT.leadIn, BEAT.leadOut + 0.5], [10, 22], CLAMP);

	return (
		<AbsoluteFill style={{alignItems: 'center', justifyContent: 'center'}}>
			<div
				style={{
					fontFamily: THIN,
					fontSize: 42,
					letterSpacing: spread,
					textTransform: 'uppercase',
					color: 'rgba(236,230,218,0.92)',
					opacity: o,
					transform: `scale(${drift})`,
					textShadow: '0 0 40px rgba(0,0,0,0.9), 0 0 14px rgba(255,220,170,0.35)',
				}}
			>
				Algunos nacen para la gloria
			</div>
		</AbsoluteFill>
	);
};

/** The title hit: PEDAZO DE / MANCO. */
export const TitleBlock: React.FC = () => {
	const frame = useCurrentFrame();
	const {fps} = useVideoConfig();
	const t = useSeconds();

	const hitFrame = BEAT.hit * fps;
	if (frame < hitFrame) {
		return null;
	}

	const elapsed = since(t, BEAT.hit);

	const s = spring({
		frame: frame - hitFrame,
		fps,
		config: {damping: 12, mass: 0.6, stiffness: 200},
	});
	const scale = interpolate(s, [0, 1], [1.45, 1]);
	const blur = interpolate(s, [0, 0.5], [30, 0], CLAMP);
	const opacity = interpolate(elapsed, [0, 0.07], [0, 1], CLAMP);

	// aberration spikes on impact, then settles to a permanent shimmer of it
	const aberration = 18 * decay(elapsed, 6) + 1.1 + 0.5 * Math.sin(t * 3.1);

	// two shimmer passes across the metal
	const shimmer = interpolate(
		elapsed,
		[0.35, 1.5, 2.6, 3.7],
		[-60, 150, -60, 150],
		CLAMP
	);

	// the second line arrives a hair after the first
	const leadIn = interpolate(elapsed, [0.06, 0.3], [0, 1], {
		...CLAMP,
		easing: Easing.out(Easing.cubic),
	});

	return (
		<AbsoluteFill
			style={{
				alignItems: 'center',
				justifyContent: 'center',
				opacity,
				transform: `scale(${scale})`,
				filter: blur > 0.2 ? `blur(${blur}px)` : undefined,
			}}
		>
			<div style={{display: 'flex', flexDirection: 'column', alignItems: 'center'}}>
				<div
					style={{
						opacity: leadIn,
						transform: `translateY(${(1 - leadIn) * -28}px)`,
						marginBottom: -18,
					}}
				>
					<MetalWord
						size={96}
						spacing={26}
						fill={SILVER}
						aberration={aberration * 0.6}
						shimmer={null}
					>
						Pedazo de
					</MetalWord>
				</div>
				<MetalWord size={340} spacing={6} fill={GOLD} aberration={aberration} shimmer={shimmer}>
					Manco
				</MetalWord>
			</div>
		</AbsoluteFill>
	);
};

/** The punchline card that stamps in underneath. */
export const Subtitle: React.FC = () => {
	const frame = useCurrentFrame();
	const {fps} = useVideoConfig();
	const t = useSeconds();

	const startFrame = BEAT.subtitle * fps;
	if (frame < startFrame) {
		return null;
	}

	const elapsed = since(t, BEAT.subtitle);
	const s = spring({
		frame: frame - startFrame,
		fps,
		config: {damping: 14, mass: 0.5, stiffness: 240},
	});
	const scale = interpolate(s, [0, 1], [1.7, 1]);
	const opacity = interpolate(elapsed, [0, 0.06], [0, 1], CLAMP);
	const blur = interpolate(s, [0, 0.6], [14, 0], CLAMP);
	const rule = interpolate(s, [0, 1], [0, 340], CLAMP);

	return (
		<AbsoluteFill style={{alignItems: 'center', justifyContent: 'center'}}>
			<div
				style={{
					opacity,
					// translate BEFORE scale, and never via margin: a margin on a
					// flex child is folded into justify-content centring, so it only
					// shifts the card by half of what you asked for.
					transform: `translateY(320px) scale(${scale})`,
					filter: blur > 0.2 ? `blur(${blur}px)` : undefined,
					display: 'flex',
					flexDirection: 'column',
					alignItems: 'center',
					gap: 14,
				}}
			>
				<div
					style={{
						width: rule,
						height: 2,
						background:
							'linear-gradient(90deg, transparent, rgba(255,206,110,0.95), transparent)',
					}}
				/>
				<div
					style={{
						fontFamily: THIN,
						fontSize: 34,
						letterSpacing: 15,
						textTransform: 'uppercase',
						color: '#e6d6ac',
						textShadow: '0 0 30px rgba(0,0,0,0.95), 0 0 18px rgba(255,190,80,0.4)',
					}}
				>
					Basada en hechos reales
				</div>
				<div
					style={{
						width: rule,
						height: 2,
						background:
							'linear-gradient(90deg, transparent, rgba(255,206,110,0.95), transparent)',
					}}
				/>
			</div>
		</AbsoluteFill>
	);
};
