from typing import Dict, List, Union

import argparse
from datetime import datetime
import logging
import os
import sys
from pytz import timezone

import discord
from discord import app_commands
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build, Resource
from googleapiclient.errors import HttpError
from ironforgedbot.common import point_values
from ironforgedbot.storage.types import IngotsStorage, Member, StorageError
from ironforgedbot.storage.sheets import SheetsStorage


# Don't care about line length for URLs & constants.
# pylint: disable=line-too-long
HISCORES_PLAYER_URL = 'https://secure.runescape.com/m=hiscore_oldschool/index_lite.ws?player={player}'
LEVEL_99_EXPERIENCE = 13034431
# Length of expected response from Runescape API. Since it returns a list,
# we can only have confidence in computed scores when response length
# matches those in the common.point_values structures.
HISCORES_EXPECTED_LENGTH = 99
# pylint: enable=line-too-long


def read_dotenv(path: str) -> Dict[str, str]:
    """Read config from a file of k=v entries."""
    config = {}
    with open(path, 'r') as f:
        for line in f:
            tmp = line.partition('=')
            config[tmp[0]] = tmp[2].removesuffix('\n')

    return config


def validate_initial_config(config: Dict[str, str]) -> bool:
    if config.get('SHEETID') is None:
        logging.error('validation failed; SHEETID required but not present in env')
        return False
    if config.get('GUILDID') is None:
        logging.error('validation failed; GUILDID required but not present in env')
        return False
    if config.get('BOT_TOKEN') is None:
        logging.error('validation failed; ' +
              'BOT_TOKEN required but not present in env')
        return False

    return True


def validate_player_name(player: str) -> bool:
    if len(player) > 12:
        return False
    return True


def compute_clan_icon(points: int):
    """Determine Icon name to include in response."""
    if points >= 13000:
        return "Myth"
    if points >= 9000:
        return "Legend"
    if points >= 5000:
        return "Dragon"
    if points >= 3000:
        return "Rune"
    if points >= 1500:
        return "Adamant"
    if points >= 700:
        return "Mithril"
    return "Iron"


def check_role(member: discord.Member, checked_role: str) -> bool:
    """Check if a member has a leadership role."""
    roles = member.roles
    for role in roles:
        if role.name == checked_role:
            return True

    return False


class DiscordClient(discord.Client):
    """Client class for bot to handle slashcommands.

    There is a chicken&egg relationship between a discord client & the
    command tree. The tree needs a client during init, but the easiest
    way to upload commands is during client.setup_hook. So, initialize
    the client first, then the tree, then add the tree property before
    calling client.run.

    intents = discord.Intents.default()
    guild = discord.Object(id=$ID)
    client = DiscordClient(intents=intents, upload=true, guild=guild)
    tree = BuildCommandTree(client)
    client.tree = tree

    client.run('token')

    Attributes:
        upload: Whether or not to upload commands to guild.
        guild: Guild for this client to upload commands to.
        tree: CommandTree to use for uploading commands.
    """
    def __init__(
        self, *, intents: discord.Intents, upload: bool,
        guild: discord.Object):
        super().__init__(intents=intents)
        self.upload=upload
        self.guild=guild

    @property
    def tree(self):
        return self._tree

    @tree.setter
    def tree(self, value: app_commands.CommandTree):
        self._tree = value

    async def setup_hook(self):
        # Copy commands to the guild (Discord server)
        # TODO: Move this to a separate CLI solely for uploading commands.
        if self.upload:
            self._tree.copy_global_to(guild=self.guild)
            await self._tree.sync(guild=self.guild)

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        print('------')


class IronForgedCommands:
    def __init__(
        self,
        tree: discord.app_commands.CommandTree,
        discord_client: DiscordClient,
        # TODO: replace sheets client with a storage interface &
        # pass in a sheets impl.
        storage_client: IngotsStorage,
        tmp_dir_path: str):
        self._tree = tree
        self._discord_client = discord_client
        self._storage_client = storage_client
        self._tmp_dir_path = tmp_dir_path

        # Description only sets the brief description.
        # Arg descriptions are pulled from function definition.
        score_command = app_commands.Command(
            name="score",
            description="Compute clan score for a player.",
            callback=self.score,
            nsfw=False,
            parent=None,
            auto_locale_strings=True,
        )
        self._tree.add_command(score_command)

        breakdown_command = app_commands.Command(
            name="breakdown",
            description="Get full breakdown of score for a player.",
            callback=self.breakdown,
            nsfw=False,
            parent=None,
            auto_locale_strings=True,
        )
        self._tree.add_command(breakdown_command)

        ingots_command = app_commands.Command(
            name="ingots",
            description="View current ingots for a player.",
            callback=self.ingots,
            nsfw=False,
            parent=None,
            auto_locale_strings=True,
        )
        self._tree.add_command(ingots_command)

        addingots_command = app_commands.Command(
            name="addingots",
            description="Add or remove ingots to a player.",
            callback=self.addingots,
            nsfw=False,
            parent=None,
            auto_locale_strings=True,
        )
        self._tree.add_command(addingots_command)

        addingotsbulk_command = app_commands.Command(
            name="addingotsbulk",
            description="Add or remove ingots to multiple players.",
            callback=self.addingotsbulk,
            nsfw=False,
            parent=None,
            auto_locale_strings=True,
        )
        self._tree.add_command(addingotsbulk_command)

        updateingots_command = app_commands.Command(
            name="updateingots",
            description="Set a player's ingot count to a new value.",
            callback=self.updateingots,
            nsfw=False,
            parent=None,
            auto_locale_strings=True,
        )
        self._tree.add_command(updateingots_command)

        syncmembers_command = app_commands.Command(
            name="syncmembers",
            description="Sync members of Discord server with ingots storage.",
            callback=self.syncmembers,
            nsfw=False,
            parent=None,
            auto_locale_strings=True,
        )
        self._tree.add_command(syncmembers_command)

    # TODO: Add tests for error cases.

    async def score(self,
                    interaction: discord.Interaction,
                    player: str):
        """Compute clan score for a Runescape player name.

        Arguments:
            interaction: Discord Interaction from CommandTree.
            player: Runescape playername to look up score for.
        """
        if not validate_player_name(player):
            await interaction.response.send_message(
                f'FAILED_PRECONDITION: RSNs can only be 12 characters long.')
            return

        logging.info(f'Handling /score player:{player} on behalf of {interaction.user.nick}')
        await interaction.response.defer()

        try:
            resp = requests.get(HISCORES_PLAYER_URL.format(player=player),
                                timeout=15)
            if resp.status_code != 200:
                await interaction.followup.send(
                    f'Looking up {player} on hiscores failed. Got status code {resp.status_code}')
                return
        except requests.exceptions.RequestException as e:
            await interaction.followup.send(
                f'Encountered an error calling Runescape API: {e}')
            return

        # Omit the first line of the response, which is total level & xp.
        lines = resp.text.split('\n')[1:]
        if len(lines) != HISCORES_EXPECTED_LENGTH:
            await interaction.followup.send(
                f'Runescape API response is not expected length; the bot needs to be updated.')
            return

        points_by_skill = skill_score(lines)
        skill_points = 0
        for _, v in points_by_skill.items():
            skill_points += v

        points_by_activity = activity_score(lines)
        activity_points = 0
        for _, v in points_by_activity.items():
            activity_points += v

        points = skill_points + activity_points

        emoji_name = compute_clan_icon(points)
        icon = ''
        for emoji in self._discord_client.get_guild(
            self._discord_client.guild.id).emojis:
            if emoji.name == emoji_name:
                icon = emoji
                break
        content=f"""{player} has {points:,}{icon}
Points from skills: {skill_points:,}
Points from minigames & bossing: {activity_points:,}"""

        await interaction.followup.send(content)


    async def breakdown(
        self,
        interaction: discord.Interaction,
        player: str):
        """Compute player score with complete source enumeration.

        Arguments:
            interaction: Discord Interaction from CommandTree.
            player: Runescape username to break down clan score for.
        """
        if not validate_player_name(player):
            await interaction.response.send_message(
                f'FAILED_PRECONDITION: RSNs can only be 12 characters long.')
            return

        logging.info(f'Handling /breakdown player:{player} on behalf of {interaction.user.nick}')
        await interaction.response.defer()

        try:
            resp = requests.get(HISCORES_PLAYER_URL.format(player=player),
                                timeout=15)
            if resp.status_code != 200:
                await interaction.followup.send(
                    f'Looking up {player} on hiscores failed. Got status code {resp.status_code}')
                return
        except requests.exceptions.RequestException as e:
            await interaction.followup.send(
                f'Encountered an error calling Runescape API: {e}')
            return

        # Omit the first line of the response, which is total level & xp.
        lines = resp.text.split('\n')[1:]
        if len(lines) != HISCORES_EXPECTED_LENGTH:
            await interaction.followup.send(
                f'Runescape API response is not expected length; the bot needs to be updated.')
            return

        points_by_skill = skill_score(lines)
        skill_points = 0
        for _, v in points_by_skill.items():
            skill_points += v

        points_by_activity = activity_score(lines)
        activity_points = 0
        for _, v in points_by_activity.items():
            activity_points += v

        total_points = skill_points + activity_points

        output = '---Points from Skills---\n'
        for i in point_values.skills():
            if points_by_skill.get(i, 0) > 0:
                output += f'{i}: {points_by_skill.get(i):,}\n'
        output += (f'Total Skill Points: {skill_points:,} ' +
            f'({round((skill_points / total_points) * 100, 2)}% of total)\n\n')
        output += '---Points from Minigames & Bossing---\n'
        for i in point_values.activities():
            if points_by_activity.get(i, 0) > 0:
                output += f'{i}: {points_by_activity.get(i):,}\n'
        output += (
            f'Total Minigame & Bossing Points: {activity_points} ' +
            f'({round((activity_points / total_points) * 100, 2)}% of total)\n\n')
        output += f'Total Points: {total_points:,}\n'

        # Now we have all of the data that we need for a full point breakdown.
        # If we write a single file though, there is a potential race
        # condition if multiple users try to run breakdown at once.
        # text files are cheap - use the player name as a good-enough amount
        # of uniquity.
        path = os.path.join(self._tmp_dir_path, f'breakdown_{player}.txt')
        with open(path, 'w') as f:
            f.write(output)

        emoji_name = compute_clan_icon(total_points)
        icon = ''
        for emoji in self._discord_client.get_guild(
            self._discord_client.guild.id).emojis:
            if emoji.name == emoji_name:
                icon = emoji
                break


        with open(path, 'rb') as f:
            discord_file = discord.File(f, filename='breakdown.txt')
            await interaction.followup.send(
                f'Total Points for {player}: {total_points}{icon}\n',
                file=discord_file)


    async def ingots(
        self, interaction: discord.Interaction, player: str):
        """View ingots for a Runescape playername.

        Arguments:
            interaction: Discord Interaction from CommandTree.
            player: Runescape username to view ingot count for.
        """
        if not validate_player_name(player):
            await interaction.response.send_message(
                f'FAILED_PRECONDITION: RSNs can only be 12 characters long.')
            return

        logging.info(f'Handling /ingots player:{player} on behalf of {interaction.user.nick}')
        await interaction.response.defer()

        # Strip whitespaces from mis-typing.
        player = player.strip()
        try:
            member = self._storage_client.read_member(player.lower())
        except StorageError as e:
            await interaction.followup.send(
                f'Encountered error reading member: {e}')
            return

        if member is None:
            await interaction.followup.send(
                f'{player} not found in storage')
            return

        icon = ''
        for emoji in self._discord_client.get_guild(
            self._discord_client.guild.id).emojis:
            if emoji.name == 'Ingot':
                icon = emoji
        await interaction.followup.send(
            f'{player} has {member.ingots:,} ingots{icon}')

    async def addingots(
        self,
        interaction: discord.Interaction,
        player: str,
        ingots: int,
        reason: str = 'None'):
        """Add ingots to a Runescape alias.

        Arguments:
            interaction: Discord Interaction from CommandTree.
            player: Runescape username to add ingots to.
            ingots: number of ingots to add to this player.
        """
        # interaction.user can be a User or Member, but we can only
        # rely on permission checking for a Member.
        caller = interaction.user
        if isinstance(caller, discord.User):
            await interaction.response.send_message(
                f'PERMISSION_DENIED: {caller.name} is not in this guild.')
            return

        if not check_role(caller, "Leadership"):
            await interaction.response.send_message(
                f'PERMISSION_DENIED: {caller.name} is not in a leadership role.')
            return

        if not validate_player_name(player):
            await interaction.response.send_message(
                f'FAILED_PRECONDITION: RSNs can only be 12 characters long.')
            return

        logging.info(f'Handling /addingots player:{player} ingots:{ingots} reason:{reason} on behalf of {interaction.user.nick}')
        await interaction.response.defer()

        player = player.strip()
        try:
            member = self._storage_client.read_member(player.lower())
        except StorageError as e:
            await interaction.followup.send(
                f'Encountered error reading member: {e}')
            return

        if member is None:
            await interaction.followup.send(
                f'{player} wasn\'t found.')
            return

        member.ingots += ingots

        try:
            self._storage_client.update_members([member], caller.nick, note=reason)
        except StorageError as e:
            await interaction.followup.send(
                f'Encountered error writing ingots: {e}')
            return

        icon = ''
        for emoji in self._discord_client.get_guild(
            self._discord_client.guild.id).emojis:
            if emoji.name == 'Ingot':
                icon = emoji
        await interaction.followup.send(
            f'Added {ingots:,} ingots to {player}; reason: {reason}. They now have {member.ingots:,} ingots{icon}')

    async def addingotsbulk(
        self,
        interaction: discord.Interaction,
        players: str,
        ingots: int,
        reason: str = 'None'):
        """Add ingots to a Runescape alias.

        Arguments:
            interaction: Discord Interaction from CommandTree.
            player: Comma-separated list of Runescape usernames to add ingots to.
            ingots: number of ingots to add to this player.
        """
        # interaction.user can be a User or Member, but we can only
        # rely on permission checking for a Member.
        caller = interaction.user
        if isinstance(caller, discord.User):
            await interaction.response.send_message(
                f'PERMISSION_DENIED: {caller.name} is not in this guild.')
            return

        if not check_role(caller, "Leadership"):
            await interaction.response.send_message(
                f'PERMISSION_DENIED: {caller.name} is not in a leadership role.')
            return

        logging.info(f'Handling /addingotsbulk players:{players} ingots:{ingots} reason:{reason} on behalf of {caller.nick}')
        await interaction.response.defer()

        player_names = players.split(',')
        player_names = [player.strip() for player in player_names]
        for player in player_names:
            if not validate_player_name(player):
                await interaction.followup.send(
                    f'FAILED_PRECONDITION: {player} is longer than 12 characters.')
                return

        try:
            members = self._storage_client.read_members()
        except StorageError as e:
            await interaction.followup.send(
                f'Encountered error reading member: {e}')
            return

        output = []
        members_to_update = []
        for player in player_names:
            found = False
            for member in members:
                if member.runescape_name == player.lower():
                    found = True
                    member.ingots += ingots
                    members_to_update.append(member)
                    output.append(f'Added {ingots:,} ingots to {player}. They now have {member.ingots:,} ingots')
                    break
            if not found:
                output.append(f'{player} not found in storage.')

        try:
            self._storage_client.update_members(members_to_update, caller.nick, note=reason)
        except StorageError as e:
            await interaction.followup.send(
                f'Encountered error writing ingots: {e}')
            return

        # Our output can be larger than the interaction followup max.
        # Send it in a file to accomodate this.
        path = os.path.join(self._tmp_dir_path, f'addingotsbulk_{caller.nick}.txt')
        with open(path, 'w') as f:
            f.write('\n'.join(output))

        with open(path, 'rb') as f:
            discord_file = discord.File(f, filename='addingotsbulk.txt')
            await interaction.followup.send(
                f'Added ingots to multiple members! Reason: {reason}',
                file=discord_file)

    async def updateingots(
        self,
        interaction: discord.Interaction,
        player: str,
        ingots: int,
        reason: str = 'None'):
        """Set ingots for a Runescape alias.

        Arguments:
            interaction: Discord Interaction from CommandTree.
            player: Runescape username to view ingot count for.
            ingots: New ingot count for this user.
        """
        # interaction.user can be a User or Member, but we can only
        # rely on permission checking for a Member.
        caller = interaction.user
        if isinstance(caller, discord.User):
            await interaction.response.send_message(
                f'PERMISSION_DENIED: {caller.name} is not in this guild.')
            return

        if not check_role(caller, "Leadership"):
            await interaction.response.send_message(
                f'PERMISSION_DENIED: {caller.name} is not in a leadership role.')
            return

        if not validate_player_name(player):
            await interaction.response.send_message(
                f'FAILED_PRECONDITION: RSNs can only be 12 characters long.')
            return

        logging.info(f'Handling /updateingots player:{player} ingots:{ingots} reason:{reason} on behalf of {interaction.user.nick}')

        await interaction.response.defer()

        player = player.strip()
        try:
            member = self._storage_client.read_member(player.lower())
        except StorageError as e:
            await interaction.followup.send(
                f'Encountered error reading member: {e}')
            return

        if member is None:
            await interaction.followup.send(
                f'{player} wasn\'t found.')
            return

        member.ingots = ingots

        try:
            self._storage_client.update_members([member], caller.nick, note=reason)
        except StorageError as e:
            await interaction.followup.send(
                f'Encountered error writing ingots: {e}')
            return

        icon = ''
        for emoji in self._discord_client.get_guild(
            self._discord_client.guild.id).emojis:
            if emoji.name == 'Ingot':
                icon = emoji
        await interaction.followup.send(
            f'Set ingot count to {ingots:,} for {player}. Reason: {reason}{icon}')


    async def syncmembers(self, interaction: discord.Interaction):
        output = ''
        mutator = interaction.user
        if isinstance(mutator, discord.User):
            await interaction.response.send_message(
                f'PERMISSION_DENIED: {mutator.name} is not in this guild.')
            return

        if not check_role(mutator, "Leadership"):
            await interaction.response.send_message(
                f'PERMISSION_DENIED: {mutator.name} is not in a leadership role.')
            return

        logging.info(f'Handling /syncmembers on behalf of {interaction.user.nick}')

        await interaction.response.defer()
        # Perform a cross join between current Discord members and
        # entries in the sheet.
        # First, read all members from Discord.
        members = []
        member_ids = []
        for member in self._discord_client.get_guild(
            self._discord_client.guild.id).members:
            if check_role(member, "Member"):
                members.append(member)
                member_ids.append(member.id)
            else:
                output += f'skipped user {member.name} because they don\'t have a "Member" role\n'

        # Then, get all current entries from storage.
        try:
            existing = self._storage_client.read_members()
        except StorageError as e:
            await interaction.followup.send(
                f'Encountered error reading members: {e}')
            return

        original_length = len(existing)
        written_ids = [member.id for member in existing]

        # Now for the actual diffing.
        # First, what new members are in Discord but not the sheet?
        new_members = []
        for member in members:
            if member.id not in written_ids:
                # Don't allow users without a nickname into storage.
                if member.nick is None:
                    output += f'skipped user {member.name} because they don\'t have a nickname in Discord\n'
                    continue
                new_members.append(Member(
                    id=int(member.id), runescape_name=member.nick.lower(), ingots=0))
                output += f'added user {member.nick} because they joined\n'

        try:
            self._storage_client.add_members(
                new_members, 'User Joined Server')
        except StorageError as e:
            await interaction.followup.send(
                f'Encountered error writing new members: {e}')
            return

        # Okay, now for all the users who have left.
        leaving_members = []
        for existing_member in existing:
            if existing_member.id not in member_ids:
                leaving_members.append(existing_member)
                output += f'removed user {existing_member.runescape_name} because they left the server\n'
        try:
            self._storage_client.remove_members(
                leaving_members, 'User Left Server')
        except StorageError as e:
            await interaction.followup.send(
                f'Encountered error removing members: {e}')
            return

        # Update all users that have changed their RSN.
        changed_members = []
        for member in members:
            for existing_member in existing:
                if member.id == existing_member.id:
                    # If a member is already in storage but had their nickname
                    # unset, set rsn to their Discord name.
                    # Otherwise, sorting fails when comparing NoneType.
                    if member.nick is None:
                        if member.name != existing_member.runescape_name:
                            changed_members.append(Member(
                                id=existing_member.id,
                                runescape_name=member.name.lower(),
                                ingots=existing_member.ingots))
                    else:
                        if member.nick.lower() != existing_member.runescape_name:
                            changed_members.append(Member(
                                id=existing_member.id,
                                runescape_name=member.nick.lower(),
                                ingots=existing_member.ingots))
                            
        for changed_member in changed_members:
            output += f'updated RSN for {changed_member.runescape_name}\n'


        try:
            self._storage_client.update_members(
                changed_members, 'Name Change')
        except StorageError as e:
            await interaction.followup.send(
                f'Encountered error updating changed members: {e}')
            return

        path = os.path.join(self._tmp_dir_path, f'syncmembers_{mutator.nick}.txt')
        with open(path, 'w') as f:
            f.write(output)

        with open(path, 'rb') as f:
            discord_file = discord.File(f, filename='syncmembers.txt')
            await interaction.followup.send(
                'Successfully synced ingots storage with current members!',
                file=discord_file)


def skill_score(hiscores: List[str]) -> Dict[str, int]:
    """Compute score from skills portion of hiscores response."""
    score = {}
    skills = point_values.skills()
    for i, _ in enumerate(skills):
        line = hiscores[i].split(',')
        skill = skills[i]
        experience = int(line[2])
        points = 0
        # API response returns -1 if user is not on hiscores.
        if experience >= 0:
            if experience < LEVEL_99_EXPERIENCE:
                points = int(
                    experience / point_values.pre99SkillValue().get(
                        skill, 0))
            else:
                points = int(
                    LEVEL_99_EXPERIENCE /
                    point_values.pre99SkillValue().get(skill, 0)) + int(
                    (experience - LEVEL_99_EXPERIENCE) /
                     point_values.post99SkillValue().get(skill, 0))
        score[skill] = points

    return score


def activity_score(hiscores: List[str]) -> Dict[str, int]:
    """Compute score from activities portion of hiscores response."""
    score = {}
    skills = point_values.skills()
    activities = point_values.activities()
    for i, _ in enumerate(activities):
        line = hiscores[len(skills) + i]
        count = int(line.split(',')[1])
        activity = activities[i]
        if count > 0 and point_values.activityPointValues().get(
                activity, 0) > 0:
            score[activity] = int(
                count / point_values.activityPointValues().get(activity))

    return score


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='A discord bot for Iron Forged.')
    parser.add_argument('--dotenv_path', default='./.env', required=False,
                        help='Filepath for .env with startup k/v pairs.')
    parser.add_argument(
        '--upload_commands', action='store_true',
        help='If supplied, will upload commands to discord server.')
    parser.add_argument(
        '--tmp_dir', default='./commands_tmp', required=False,
        help='Directory path for where to store point break downs to upload to discord.')
    parser.add_argument(
        '--logfile', default='./ironforgedbot.log', required=False,
        help='Path to file to write log entries to.')
    args = parser.parse_args()

    logging.basicConfig(
        format='%(asctime)s %(message)s', filename=args.logfile,
        level=logging.INFO)

    # also log to stderr
    root = logging.getLogger()
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s %(message)s')
    handler.setFormatter(formatter)
    root.addHandler(handler)

    # Fail out early if our required args are not present.
    init_config = read_dotenv(args.dotenv_path)
    if not validate_initial_config(init_config):
        sys.exit(1)

    # TODO: We lock the bot down with oauth perms; can we shrink intents to match?
    intents = discord.Intents.default()
    intents.members = True
    guild = discord.Object(id=init_config.get('GUILDID'))
    client = DiscordClient(intents=intents, upload=args.upload_commands,
                           guild=guild)
    tree = discord.app_commands.CommandTree(client)

    storage_client = SheetsStorage.from_account_file(
        'service.json', init_config.get('SHEETID'))

    commands = IronForgedCommands(
        tree, client, storage_client, args.tmp_dir)
    client.tree = tree

    client.run(init_config.get('BOT_TOKEN'))
