import React from 'react';
import {AbsoluteFill, interpolate, random} from 'remotion';
import {BEAT, CLAMP, useSeconds} from './timing';

const RAYS = 11;

/**
 * Deep blacks, a warm floor glow, and volumetric light fanning down from above.
 * The rays brighten as the piece charges and flare on the hit.
 */
export const Backdrop: React.FC = () => {
	const t = useSeconds();

	const raysIn = interpolate(t, [BEAT.raysIn, BEAT.raysIn + 1.6], [0, 1], CLAMP);
	const charge = interpolate(t, [BEAT.charge, BEAT.hit], [0, 1], CLAMP);
	const bloom = interpolate(
		t,
		[BEAT.hit, BEAT.hit + 0.25, BEAT.hit + 1.6],
		[0, 1, 0.35],
		CLAMP
	);
	const sway = Math.sin(t * 0.45);

	return (
		<AbsoluteFill>
			<AbsoluteFill
				style={{
					background:
						'radial-gradient(120% 90% at 50% 52%, #14110c 0%, #080806 42%, #000000 100%)',
				}}
			/>

			{/* warm light pooling on the floor of the frame */}
			<AbsoluteFill
				style={{
					background: `radial-gradient(75% 48% at 50% 100%, rgba(255,146,38,${
						0.09 + charge * 0.06 + bloom * 0.2
					}) 0%, transparent 72%)`,
				}}
			/>

			{/* volumetric rays */}
			<AbsoluteFill
				style={{
					opacity: raysIn * (0.5 + charge * 0.8 + bloom * 0.6),
					filter: 'blur(28px)',
					mixBlendMode: 'screen',
				}}
			>
				{new Array(RAYS).fill(0).map((_, i) => {
					const seed = random(`ray-${i}`);
					const angle = -47 + (94 / (RAYS - 1)) * i + sway * 2.6;
					const w = 38 + seed * 120;
					const flicker = 0.62 + 0.38 * Math.sin(t * (0.7 + seed * 1.6) + seed * 11);
					const o = (0.09 + 0.2 * random(`rayo-${i}`)) * flicker;
					return (
						<div
							key={i}
							style={{
								position: 'absolute',
								top: '-32%',
								left: '50%',
								width: w,
								height: '155%',
								transformOrigin: '50% 0%',
								transform: `translateX(-50%) rotate(${angle}deg)`,
								background: `linear-gradient(to bottom, rgba(255,224,176,${o}) 0%, rgba(255,188,116,${
									o * 0.34
								}) 46%, transparent 86%)`,
							}}
						/>
					);
				})}
			</AbsoluteFill>

			{/* the light the title is about to arrive on: a hairline that charges up */}
			<Hairline />
		</AbsoluteFill>
	);
};

/** A thin horizontal seam of light that grows through the tension beat and
 *  blows out at the hit - the seed the title bursts from. */
const Hairline: React.FC = () => {
	const t = useSeconds();
	if (t >= BEAT.hit + 0.35) {
		return null;
	}

	const grow = interpolate(t, [BEAT.leadIn, BEAT.charge, BEAT.hit], [0, 26, 78], CLAMP);
	const bright = interpolate(t, [BEAT.leadIn, BEAT.charge, BEAT.compress, BEAT.hit], [0, 0.35, 0.7, 1], CLAMP);
	const blowout = interpolate(t, [BEAT.hit, BEAT.hit + 0.3], [1, 0], CLAMP);
	const jitter = 1 + 0.06 * Math.sin(t * 40) * interpolate(t, [BEAT.charge, BEAT.hit], [0, 1], CLAMP);

	return (
		<AbsoluteFill style={{alignItems: 'center', justifyContent: 'center', mixBlendMode: 'screen'}}>
			<div
				style={{
					width: `${grow}%`,
					height: 3 * jitter,
					opacity: bright * blowout,
					background:
						'linear-gradient(90deg, transparent 0%, rgba(255,214,140,0.9) 22%, #fff6de 50%, rgba(255,214,140,0.9) 78%, transparent 100%)',
					filter: `blur(${2 + bright * 5}px)`,
					boxShadow: `0 0 ${40 + bright * 90}px rgba(255,190,90,${bright * 0.8})`,
				}}
			/>
		</AbsoluteFill>
	);
};

/** Lens vignette. Lives outside the camera transform - it belongs to the lens,
 *  not to the scene. */
export const Vignette: React.FC = () => {
	const t = useSeconds();
	const tighten = interpolate(t, [BEAT.charge, BEAT.hit], [0, 0.22], CLAMP);
	const relax = interpolate(t, [BEAT.hit, BEAT.hit + 1.2], [0, 0.12], CLAMP);
	const strength = 0.72 + tighten - relax;

	return (
		<AbsoluteFill
			style={{
				background: `radial-gradient(78% 68% at 50% 50%, transparent 38%, rgba(0,0,0,${strength}) 100%)`,
				pointerEvents: 'none',
			}}
		/>
	);
};

/** The anticipation: the whole frame dims hard in the moments before the hit. */
export const Compression: React.FC = () => {
	const t = useSeconds();
	const dim = interpolate(
		t,
		[BEAT.compress, BEAT.hit - 0.02, BEAT.hit],
		[0, 0.62, 0],
		CLAMP
	);
	if (dim <= 0) {
		return null;
	}
	return <AbsoluteFill style={{backgroundColor: '#000', opacity: dim}} />;
};

/** Fade to black on the outro. */
export const FadeOut: React.FC = () => {
	const t = useSeconds();
	const o = interpolate(t, [BEAT.fade, BEAT.black], [0, 1], CLAMP);
	if (o <= 0) {
		return null;
	}
	return <AbsoluteFill style={{backgroundColor: '#000', opacity: o}} />;
};
