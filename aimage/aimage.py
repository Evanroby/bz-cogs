import asyncio
import logging
from collections import defaultdict
from copy import copy
from typing import List, Union

import aiohttp
import discord
from rapidfuzz import fuzz
from redbot.core import Config, app_commands, checks, commands
from redbot.core.bot import Red
from redbot.core.i18n import Translator, cog_i18n

from aimage.abc import CompositeMetaClass
from aimage.common.constants import DEFAULT_BADWORDS_BLACKLIST, DEFAULT_NEGATIVE_PROMPT, API_Type
from aimage.common.params import ImageGenParams
from aimage.image_handler import ImageHandler
from aimage.settings import Settings

logger = logging.getLogger("red.bz_cogs.aimage")

_ = Translator("AImage", __file__)


@cog_i18n(_)
class AImage(Settings, ImageHandler, commands.Cog, metaclass=CompositeMetaClass):
    """Generate AI images using a A1111 endpoint"""

    __version__ = "2.0"
    __author__ = "zhaobenny"
    __contributor__ = "evanroby"

    def __init__(self, bot):
        super().__init__()
        self.bot: Red = bot
        self.config = Config.get_conf(self, identifier=75567113)

        default_guild = {
            "endpoint": None,
            "api_type": API_Type.AUTOMATIC1111.value,
            "nsfw": True,
            "words_blacklist": DEFAULT_BADWORDS_BLACKLIST,
            "negative_prompt": DEFAULT_NEGATIVE_PROMPT,
            "cfg": 7,
            "sampling_steps": 20,
            "sampler": "Euler a",
            "checkpoint": None,
            "vae": None,
            "adetailer": False,
            "tiledvae": False,
            "width": 512,
            "height": 512,
            "max_img2img": 1536,
            "auth": None,
        }

        self.session = aiohttp.ClientSession()
        self.generating = defaultdict(lambda: False)
        self.autocomplete_cache = defaultdict(dict)

        self.config.register_guild(**default_guild)

    def format_help_for_context(self, ctx):
        pre_processed = super().format_help_for_context(ctx)
        n = "\n" if "\n\n" not in pre_processed else ""
        return (
            f"{pre_processed}{n}\n"
            f"{_('Cog Version')}: {self.__version__}\n"
            f"{_('Cog Author')}: {self.__author__}\n"
            f"{_('Cog Contributor')}: {self.__contributor__}"
        )

    async def red_delete_data_for_user(self, **kwargs):
        return

    async def cog_unload(self):
        await self.session.close()

    async def object_autocomplete(
        self, interaction: discord.Interaction, current: str, choices: list
    ) -> List[app_commands.Choice[str]]:
        if not choices:
            await self._update_autocomplete_cache(interaction)
            return []

        choices = self.filter_list(choices, current)

        return [app_commands.Choice(name=choice, value=choice) for choice in choices[:25]]

    async def samplers_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        choices = self.autocomplete_cache[interaction.guild_id].get("samplers") or []
        return await self.object_autocomplete(interaction, current, choices)

    async def loras_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        choices = self.autocomplete_cache[interaction.guild_id].get("loras") or []

        if current:
            current_loras = current.split(" ")
            if any(
                part in current_loras for part in choices
            ):  # TODO: currently only works with lora value of 1
                new_choices = []
                for choice in choices:
                    choice_parts = choice.split(" ")
                    if any(part in current_loras for part in choice_parts):
                        continue
                    new_choices.append(current + " " + choice)
                choices = new_choices

        return await self.object_autocomplete(interaction, current, choices)

    async def style_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        choices = self.autocomplete_cache[interaction.guild_id].get("styles") or []

        if current:
            current_styles = current.split(",")
            if any(part in current_styles for part in choices):
                new_choices = []
                for choice in choices:
                    choice_parts = choice.split(", ")
                    if any(part in current_styles for part in choice_parts):
                        continue
                    new_choices.append(current + ", " + choice)
            choices = new_choices

        return await self.object_autocomplete(interaction, current, choices)

    async def checkpoint_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        choices = self.autocomplete_cache[interaction.guild_id].get("checkpoints") or []
        return await self.object_autocomplete(interaction, current, choices)

    async def vae_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        choices = self.autocomplete_cache[interaction.guild_id].get("vaes") or []
        return await self.object_autocomplete(interaction, current, choices)

    @staticmethod
    def filter_list(options: list, current: str):
        results = []

        ratios = [(item, fuzz.partial_ratio(current.lower(), item.lower())) for item in options]

        sorted_options = sorted(ratios, key=lambda x: x[1], reverse=True)

        for item, _ in sorted_options:
            results.append(item)
        return results

    _parameter_descriptions = {
        "prompt": _("The prompt to generate an image from."),
        "negative_prompt": _("Undesired terms go here."),
        "style": _("Style to use"),
        "cfg": _("Sets the intensity of the prompt, 7 is common."),
        "sampler": _("The algorithm which guides image generation."),
        "steps": _("How many sampling steps, 20-30 is common."),
        "seed": _("Random number that generates the image, -1 for random."),
        "variation": _("Finetunes details within the same seed, 0.05 is common."),
        "variation_seed": _("This subseed guides the variation, -1 for random."),
        "checkpoint": _("The main AI model used to generate the image."),
        "vae": _("The VAE converts the final details of the image."),
        "lora": _("Shortcut to insert LoRA into the prompt."),
    }

    _parameter_autocompletes = {
        "sampler": samplers_autocomplete,
        "lora": loras_autocomplete,
        "checkpoint": checkpoint_autocomplete,
        "vae": vae_autocomplete,
        "style": style_autocomplete,
    }

    @commands.command()
    @commands.cooldown(1, 10, commands.BucketType.default)
    @checks.bot_has_permissions(attach_files=True)
    @checks.bot_in_a_guild()
    async def imagine(self, ctx: commands.Context, *, prompt: str):
        """
        Generate an image

        **Arguments**
            - `prompt` a prompt to generate an image from
        """
        if not self.autocomplete_cache[ctx.guild.id]:
            asyncio.create_task(self._update_autocomplete_cache(ctx))

        params = ImageGenParams(prompt=prompt)
        await self.generate_image(ctx, params=params)

    @app_commands.command(name="imagine")
    @app_commands.describe(
        width=_("Default image width is 512, or 1024 for SDXL."),
        height=_("Default image height is 512, or 1024 for SDXL."),
        **_parameter_descriptions,
    )
    @app_commands.autocomplete(**_parameter_autocompletes)
    @app_commands.checks.cooldown(1, 10, key=None)
    @app_commands.checks.bot_has_permissions(attach_files=True)
    @app_commands.guild_only()
    async def imagine_app(
        self,
        interaction: discord.Interaction,
        prompt: str,
        negative_prompt: str = None,
        style: str = None,
        width: app_commands.Range[int, 256, 1536] = None,
        height: app_commands.Range[int, 256, 1536] = None,
        cfg: app_commands.Range[float, 1, 30] = None,
        sampler: str = None,
        steps: app_commands.Range[int, 1, 150] = None,
        seed: app_commands.Range[int, -1, None] = -1,
        variation: app_commands.Range[float, 0, 1] = 0,
        variation_seed: app_commands.Range[int, -1, None] = -1,
        checkpoint: str = None,
        vae: str = None,
        lora: str = "",
    ):
        """
        Generate an image using AI.
        """
        await interaction.response.defer(thinking=True)

        ctx: commands.Context = await self.bot.get_context(interaction)  # noqa
        if not await self._can_run_command(ctx, "imagine"):
            return await interaction.followup.send(
                _("You do not have permission to do this."), ephemeral=True
            )

        params = ImageGenParams(
            prompt=prompt,
            negative_prompt=negative_prompt,
            style=style,
            width=width,
            height=height,
            cfg=cfg,
            sampler=sampler,
            steps=steps,
            seed=seed,
            variation=variation,
            variation_seed=variation_seed,
            checkpoint=checkpoint,
            vae=vae,
            lora=lora,
        )

        await self.generate_image(interaction, params=params)

    @app_commands.command(name="reimagine")
    @app_commands.describe(
        image=_("The image to reimagine with AI."),
        denoising=_("How much the image should change. Try around 0.6"),
        scale=_("Resizes the image up or down, 0.5 to 2.0."),
        **_parameter_descriptions,
    )
    @app_commands.autocomplete(**_parameter_autocompletes)
    @app_commands.checks.cooldown(1, 10, key=None)
    @app_commands.checks.bot_has_permissions(attach_files=True)
    @app_commands.guild_only()
    async def reimagine_app(
        self,
        interaction: discord.Interaction,
        image: discord.Attachment,
        denoising: app_commands.Range[float, 0, 1],
        prompt: str,
        negative_prompt: str = None,
        style: str = None,
        scale: app_commands.Range[float, 0.5, 2.0] = 1,
        cfg: app_commands.Range[float, 1, 30] = None,
        sampler: str = None,
        steps: app_commands.Range[int, 1, 150] = None,
        seed: app_commands.Range[int, -1, None] = -1,
        variation: app_commands.Range[float, 0, 1] = 0,
        variation_seed: app_commands.Range[int, -1, None] = -1,
        checkpoint: str = None,
        vae: str = None,
        lora: str = "",
    ):
        """
        Convert an image using AI.
        """
        await interaction.response.defer(thinking=True)

        ctx: commands.Context = await self.bot.get_context(interaction)  # noqa
        if not await self._can_run_command(ctx, "imagine"):
            return await interaction.followup.send(
                _("You do not have permission to do this."), ephemeral=True
            )

        if not image.content_type.startswith("image/"):
            return await interaction.followup.send(
                _("The file you uploaded is not a valid image."), ephemeral=True
            )

        size = image.width * image.height * scale * scale
        maxsize = (await self.config.guild(ctx.guild).max_img2img()) ** 2
        if size > maxsize:
            return await interaction.followup.send(
                _(
                    "Max img2img size is {max_size}² pixels. "
                    "Your image {resize_text} {image_size}² pixels, which is too big."
                ).format(
                    max_size=int(maxsize**0.5),
                    resize_text=_("after resizing would be") if scale != 0 else _("is"),
                    image_size=int(size**0.5),
                ),
                ephemeral=True,
            )

        params = ImageGenParams(
            prompt=prompt,
            negative_prompt=negative_prompt,
            style=style,
            cfg=cfg,
            sampler=sampler,
            steps=steps,
            seed=seed,
            variation=variation,
            variation_seed=variation_seed,
            checkpoint=checkpoint,
            vae=vae,
            lora=lora,
            # img2img
            height=image.height * scale,
            width=image.width * scale,
            init_image=await image.read(),
            denoising=denoising,
        )

        await self.generate_img2img(interaction, params=params)

    async def _can_run_command(self, ctx: commands.Context, command_name: str) -> bool:
        prefix = await self.bot.get_prefix(ctx.message)
        prefix = prefix[0] if isinstance(prefix, list) else prefix
        fake_message = copy(ctx.message)
        fake_message.content = prefix + command_name
        command = ctx.bot.get_command(command_name)
        fake_context: commands.Context = await ctx.bot.get_context(fake_message)  # noqa
        try:
            can = await command.can_run(
                fake_context, check_all_parents=True, change_permission_state=False
            )
        except commands.CommandError:
            can = False
        return can

    async def get_api_instance(self, ctx: Union[commands.Context, discord.Interaction]):
        api_type = await self.config.guild(ctx.guild).api_type()
        if api_type == API_Type.AUTOMATIC1111.value:
            from aimage.apis.a1111 import A1111

            instance = A1111(self, ctx)
        elif api_type == API_Type.AIHorde.value:
            from aimage.apis.aihorde import AIHorde

            instance = AIHorde(self, ctx)
        await instance._init()
        return instance

    async def _update_autocomplete_cache(self, ctx: Union[commands.Context, discord.Interaction]):
        api = await self.get_api_instance(ctx)
        try:
            logger.debug(
                _("Ran a update to get possible autocomplete terms in server {guild_id}").format(
                    guild_id=ctx.guild.id
                )
            )
            await api.update_autocomplete_cache(self.autocomplete_cache)
        except NotImplementedError:
            logger.debug(
                _("Autocomplete terms is not supported by the api in server {guild_id}").format(
                    guild_id=ctx.guild.id
                )
            )
            pass
