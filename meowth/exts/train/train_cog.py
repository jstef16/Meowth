from meowth import Cog, command, bot, checks
from meowth.exts.map import Gym, ReportChannel, Mapper
from meowth.exts.raid import Raid
from meowth.exts.users import MeowthUser
from meowth.utils import formatters, snowflake
from meowth.utils.converters import ChannelMessage

import asyncio
from datetime import datetime
from math import ceil
import typing

class Train:

    instances = dict()
    by_channel = dict()
    by_message = dict()

    def __new__(cls, train_id, *args, **kwargs):
        if train_id in cls.instances:
            return cls.instances[train_id]
        instance = super().__new__(cls)
        cls.instances[train_id] = instance
        return instance

    def __init__(self, train_id, bot, guild_id, channel_id, report_channel_id):
        self.id = train_id
        self.bot = bot
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.report_channel_id = report_channel_id
        self.current_raid = None
        self.next_raid = None
        self.done_raids = []
        self.report_msg_ids = []
        self.multi_msg_ids = []
        self.message_id = None
        self.trainer_dict = {}
    
    def to_dict(self):
        d = {
            'id': self.id,
            'guild_id': self.guild_id,
            'channel_id': self.channel_id,
            'report_channel_id': self.report_channel_id,
            'current_raid_id': self.current_raid.id if self.current_raid else None,
            'next_raid_id': self.next_raid.id if self.next_raid else None,
            'done_raid_ids': [x.id for x in self.done_raids],
            'report_msg_ids': self.report_msg_ids,
            'multi_msg_ids': self.multi_msg_ids,
            'message_id': self.message_id
        }
        return d
    
    @property
    def _data(self):
        table = self.bot.dbi.table('trains')
        query = table.query.where(id=self.id)
        return query
    
    @property
    def _insert(self):
        table = self.bot.dbi.table('trains')
        insert = table.insert
        d = self.to_dict()
        insert.row(**d)
        return insert

    async def upsert(self):
        insert = self._insert
        await insert.commit(do_update=True)
    
    @property
    def guild(self):
        return self.bot.get_guild(self.guild_id)
    
    @property
    def channel(self):
        return self.bot.get_channel(self.channel_id)
    
    async def report_message(self):
        try:
            msg = await self.report_channel.channel.get_message(self.message_id)
            return msg
        except:
            return None
    
    @property
    def report_channel(self):
        rchan = self.bot.get_channel(self.report_channel_id)
        return ReportChannel(self.bot, rchan)
    
    async def report_msgs(self):
        for msgid in self.report_msg_ids:
            try:
                msg = await self.channel.get_message(msgid)
                if msg:
                    yield msg
            except:
                continue
        
    async def clear_reports(self):
        async for msg in self.report_msgs():
            await msg.delete()
        self.report_msg_ids = []
    
    async def multi_msgs(self):
        for msgid in self.multi_msg_ids:
            try:
                chn, msg = await ChannelMessage.from_id_string(self.bot, msgid)
                if msg:
                    yield msg
            except:
                continue
    
    async def clear_multis(self):
        async for msg in self.multi_msgs():
            await msg.delete()
        self.multi_msg_ids = []
    
    async def reported_raids(self):
        for msgid in self.report_msg_ids:
            raid = Raid.by_trainreport.get(msgid)
            msg = await self.channel.get_message(msgid)
            yield (msg, raid)
    
    async def report_results(self):
        async for msg, raid in self.reported_raids():
            reacts = msg.reactions
            for react in reacts:
                if react.emoji != '\u2b06':
                    continue
                count = react.count
                yield (raid, count)
    
    async def possible_raids(self):
        idlist = await self.report_channel.get_all_raids()
        return [Raid.instances.get(x) for x in idlist]
    
    async def select_raid(self, raid):
        raid.channel_ids.append(str(self.channel_id))
        if raid.status == 'active':
            embed = await raid.raid_embed()
        elif raid.status == 'egg':
            embed = await raid.egg_embed()
        elif raid.status == 'hatched':
            embed = await raid.hatched_embed()
        raidmsg = await self.channel.send(embed=embed)
        react_list = raid.react_list
        for react in react_list:
            if isinstance(react, int):
                react = self.bot.get_emoji(react)
            await raidmsg.add_reaction(react)
        idstring = f'{self.channel_id}/{raidmsg.id}'
        raid.message_ids.append(idstring)
        await raid.upsert()
        Raid.by_message[idstring] = raid
        Raid.by_channel[str(self.channel_id)] = raid
        self.current_raid = raid
        self.next_raid = None
        await self.upsert()
        self.bot.loop.create_task(self.poll_next_raid())
    
    async def finish_current_raid(self):
        raid = self.current_raid
        self.done_raids.append(raid)
        raid.channel_ids.remove(str(self.channel_id))
        for msgid in raid.message_ids:
            if msgid.startswith(str(self.channel_id)):
                try:
                    chn, msg = await ChannelMessage.from_id_string(self.bot, msgid)
                    await msg.delete()
                except:
                    pass
                raid.message_ids.remove(msgid)
        await raid.upsert()
        if not self.poll_task.done():
            self.poll_task.cancel()
            self.next_raid = await self.poll_task
        await self.clear_reports()
        await self.clear_multis()
        await self.select_raid(self.next_raid)

    async def get_trainer_dict(self):
        def data(rcrd):
            trainer = rcrd['user_id']
            party = rcrd.get('party', [0,0,0,1])
            return trainer, party
        trainer_dict = {}
        user_table = self.bot.dbi.table('train_rsvp')
        query = user_table.query()
        query.where(train_id=self.id)
        rsvp_data = await query.get()
        for rcrd in rsvp_data:
            trainer, party = data(rcrd)
            trainer_dict[trainer] = party
        return trainer_dict

    @staticmethod
    def team_dict(trainer_dict):
        d = {
            'mystic': 0,
            'instinct': 0,
            'valor': 0,
            'unknown': 0
        }
        for trainer in trainer_dict:
            bluecount = trainer_dict[trainer][0]
            yellowcount = trainer_dict[trainer][1]
            redcount = trainer_dict[trainer][2]
            unknowncount = trainer_dict[trainer][3]
            d['mystic'] += bluecount
            d['instinct'] += yellowcount
            d['valor'] += redcount
            d['unknown'] += unknowncount
        return d

    @property
    def team_str(self):
        team_str = self.team_string(self.bot, self.trainer_dict)
        return team_str

    @staticmethod
    def team_string(bot, trainer_dict):
        team_dict = Train.team_dict(trainer_dict)
        team_str = f"{bot.config.team_emoji['mystic']}: {team_dict['mystic']} | "
        team_str += f"{bot.config.team_emoji['instinct']}: {team_dict['instinct']} | "
        team_str += f"{bot.config.team_emoji['valor']}: {team_dict['valor']} | "
        team_str += f"{bot.config.team_emoji['unknown']}: {team_dict['unknown']}"
        return team_str

    async def select_first_raid(self, author):
        raids = await self.possible_raids()
        react_list = formatters.mc_emoji(len(raids))
        content = "Select your first raid from the list below!"
        async for embed in self.display_choices(raids, react_list):
            multi = await self.report_channel.channel.send(content, embed=embed)
            content = ""
            self.multi_msg_ids.append(f'{self.report_channel_id}/{multi.id}')
        payload = await formatters.ask(self.bot, [multi], user_list=[author.id], 
            react_list=react_list)
        choice_dict = dict(zip(react_list, raids))
        first_raid = choice_dict[str(payload.emoji)]
        await self.clear_multis()
        await self.select_raid(first_raid)
    
    async def poll_next_raid(self):
        raids = await self.possible_raids()
        if self.current_raid:
            raids.remove(self.current_raid)
        raids = [x for x in raids if x not in self.done_raids and x.status != 'expired']
        react_list = formatters.mc_emoji(len(raids))
        content = "Vote on the next raid from the list below!"
        async for embed in self.display_choices(raids, react_list):
            multi = await self.channel.send(content, embed=embed)
            content = ""
            self.multi_msg_ids.append(f'{self.channel_id}/{multi.id}')
        await self.upsert()
        self.poll_task = self.bot.loop.create_task(self.get_poll_results(multi, raids, react_list))
        
    async def get_poll_results(self, multi, raids, react_list):
        multitask = self.bot.loop.create_task(formatters.poll(self.bot, [multi],
            react_list=react_list))
        try:
            results = await multitask
        except asyncio.CancelledError:
            multitask.cancel()
            results = await multitask
        if results:
            emoji = results[0][0]
            count = results[0][1]
        else:
            emoji = None
            count = 0
        report_results = [(x, y) async for x, y in self.report_results()]
        if report_results:
            sorted_reports = sorted(report_results, key=lambda x: x[1], reverse=True)
            report_max = sorted_reports[0][1]
        else:
            report_max = 0
        if report_max and report_max >= count:
            return sorted_reports[0][0]
        elif emoji:
            choice_dict = dict(zip(react_list, raids))
            return choice_dict[str(emoji)]
        else:
            return 
    
    async def display_choices(self, raids, react_list):
        dest_dict = {}
        eggs_list = []
        hatched_list = []
        active_list = []
        if self.current_raid:
            if isinstance(self.current_raid.gym, Gym):
                origin = self.current_raid.gym.id
                known_dest_ids = [x.id for x in raids if isinstance(x.gym, Gym)]
                dests = [Raid.instances[x].gym.id for x in known_dest_ids]
                times = await Mapper.get_travel_times(self.bot, [origin], dests)
                dest_dict = {}
                for d in times:
                    if d['origin_id'] == origin and d['dest_id'] in dests:
                        dest_dict[d['dest_id']] = d['travel_time']
        urls = {x.id: await self.route_url(x) for x in raids}
        react_list = formatters.mc_emoji(len(raids))
        for i in range(len(raids)):
            x = raids[i]
            e = react_list[i]
            summary = f'{e} {await x.summary_str()}'
            if x.gym.id in dest_dict:
                travel = f'Travel Time: {dest_dict[x.gym.id]//60} mins'
            else:
                travel = "Travel Time: Unknown"
            directions = f'[{travel}]({urls[x.id]})'
            summary += f"\n{directions}"
            if x.status == 'egg':
                eggs_list.append(summary)
            elif x.status == 'active' or x.status == 'hatched':
                active_list.append(summary)
        number = len(raids)
        pages = ceil(number/3)
        for i in range(pages):
            fields = {}
            left = 3
            if pages == 1:
                title = 'Raid Choices'
            else:
                title = f'Raid Choices (Page {i+1} of {pages})'
            if len(active_list) > left:
                fields['Active'] = "\n\n".join(active_list[:3])
                active_list = active_list[3:]
                embed = formatters.make_embed(title=title, fields=fields)
                yield embed
                continue
            elif active_list:
                fields['Active'] = "\n\n".join(active_list) + "\n\u200b"
                left -= len(active_list)
                active_list = []
            if not left:
                embed = formatters.make_embed(title=title, fields=fields)
                yield embed
                continue
            if not left:
                embed = formatters.make_embed(title=title, fields=fields)
                yield embed
                continue
            if len(eggs_list) > left:
                fields['Eggs'] = "\n\n".join(eggs_list[:left])
                eggs_list = eggs_list[left:]
                embed = formatters.make_embed(title=title, fields=fields)
                yield embed
                continue
            elif eggs_list:
                fields['Eggs'] = "\n\n".join(eggs_list)
            embed = formatters.make_embed(title=title, fields=fields)
            yield embed

    async def route_url(self, next_raid):
        if isinstance(next_raid.gym, Gym):
            return await next_raid.gym.url()
        else:
            return next_raid.gym.url
    
    async def new_raid(self, raid: Raid):
        embed = await RaidEmbed.from_raid(self, raid)
        content = "Use the reaction below to vote for this raid next!"
        msg = await self.channel.send(content, embed=embed.embed)
        await msg.add_reaction('\u2b06')
        
    async def update_rsvp(self, user_id, status):
        self.trainer_dict = await self.get_trainer_dict()
        msg = await self.report_message()
        if not msg:
            return
        train_embed = TrainEmbed(msg.embeds[0])
        train_embed.team_str = self.team_str
        await msg.edit(embed=train_embed.embed)
        channel = self.channel
        guild = channel.guild
        member = guild.get_member(user_id)
        if status == 'join':
            status_str = ' has joined the train!'
        elif status == 'cancel':
            status_str =' has left the train!'
        content = f'{member.display_name}{status_str}'
        embed = RSVPEmbed.from_train(self).embed
        await channel.send(content, embed=embed)
        if not self.trainer_dict:
            await self.end_train()

    
    async def end_train(self):
        await self.channel.send('This train is now empty! This channel will be deleted in one minute.')
        await asyncio.sleep(60)
        await self._data.delete()
        train_rsvp_table = self.bot.dbi.table('train_rsvp')
        query = train_rsvp_table.query
        query.where(train_id=self.id)
        await query.delete()
        del Train.by_channel[self.channel_id]
        del Train.by_message[self.message_id]
        del Train.instances[self.id]
        await self.channel.delete()
        msg = await self.report_message()
        await msg.clear_reactions()
        embed = formatters.make_embed(content="This raid train has ended!")
        await msg.edit(content="", embed=embed)
    
    @classmethod
    async def from_data(cls, bot, data):
        train_id = data['id']
        guild_id = data['guild_id']
        channel_id = data['channel_id']
        report_channel_id = data['report_channel_id']
        current_raid_id = data.get('current_raid_id')
        next_raid_id = data.get('next_raid_id')
        done_raid_ids = data.get('done_raid_ids', [])
        report_msg_ids = data.get('report_msg_ids', [])
        multi_msg_ids = data.get('multi_msg_ids', [])
        message_id = data['message_id']
        train = cls(train_id, bot, guild_id, channel_id, report_channel_id)
        train.current_raid = Raid.instances.get(current_raid_id) if current_raid_id else None
        train.next_raid = Raid.instances.get(next_raid_id) if next_raid_id else None
        train.done_raids = [Raid.instances.get(x) for x in done_raid_ids]
        train.report_msg_ids = report_msg_ids
        train.multi_msg_ids = multi_msg_ids
        train.message_id = message_id
        cls.by_channel[channel_id] = train
        cls.by_message[message_id] = train
        idstring = multi_msg_ids[-1]
        multi = await ChannelMessage.from_id_string(bot, idstring)
        raids = await train.possible_raids()
        if train.current_raid:
            raids.remove(train.current_raid)
        raids = [x for x in raids if x not in train.done_raids and x.status != 'expired']
        react_list = formatters.mc_emoji(len(raids))
        train.poll_task = bot.loop.create_task(train.get_poll_results(multi, raids, react_list))
        return train



class TrainCog(Cog):

    def __init__(self, bot):
        self.bot = bot
        self.bot.loop.create_task(self.add_listeners())
        self.bot.loop.create_task(self.pickup_traindata())
    
    async def pickup_traindata(self):
        train_table = self.bot.dbi.table('trains')
        query = train_table.query
        data = await query.get()
        for rcrd in data:
            self.bot.loop.create_task(self.pickup_train(rcrd))

    async def pickup_train(self, rcrd):
        train = await Train.from_data(self.bot, rcrd)

    
    async def add_listeners(self):
        if self.bot.dbi.train_listener:
            await self.bot.dbi.pool.release(self.bot.dbi.train_listener)
        self.bot.dbi.train_listener = await self.bot.dbi.pool.acquire()
        trainrsvp = ('train', self._rsvp)
        await self.bot.dbi.train_listener.add_listener(*trainrsvp)
    
    def _rsvp(self, connection, pid, channel, payload):
        if channel != 'train':
            return
        payload_args = payload.split('/')
        train_id = int(payload_args[0])
        train = Train.instances.get(train_id)
        if not train:
            return
        event_loop = asyncio.get_event_loop()
        if payload_args[1].isdigit():
            user_id = int(payload_args[1])
            status = payload_args[2]
            event_loop.create_task(train.update_rsvp(user_id, status))
            return
  
    @Cog.listener()
    async def on_raw_reaction_add(self, payload):
        msg_id = payload.message_id
        channel = self.bot.get_channel(payload.channel_id)
        train = Train.by_message.get(msg_id)
        if not train:
            return
        if payload.user_id == self.bot.user.id:
            return
        user = self.bot.get_user(payload.user_id)
        meowthuser = MeowthUser(self.bot, user)
        if payload.emoji.is_custom_emoji():
            emoji = payload.emoji.id
        else:
            emoji = str(payload.emoji)
        if emoji == '🚂':
            party = await meowthuser.party()
            await self._join(meowthuser, train, party)
        elif emoji == '❌':
            await self._leave(meowthuser, train)
        msg = await channel.get_message(msg_id)
        await msg.remove_reaction(emoji, user)

    
    @command()
    async def train(self, ctx):
        report_channel = ReportChannel(self.bot, ctx.channel)
        city = await report_channel.city()
        city = city.split()[0]
        name = f'{city}-raid-train'
        cat = ctx.channel.category
        ow = dict(ctx.channel.overwrites)
        train_channel = await ctx.guild.create_text_channel(name, category=cat, overwrites=ow)
        train_id = next(snowflake.create())
        new_train = Train(train_id, self.bot, ctx.guild.id, train_channel.id, ctx.channel.id)
        await new_train.select_first_raid(ctx.author)
        Train.by_channel[train_channel.id] = new_train
        embed = await TrainEmbed.from_train(new_train)
        msg = await ctx.send(f"{ctx.author.display_name} has started a raid train! You can join by reacting to this message and coordinate in {train_channel.mention}!", embed=embed.embed)
        await msg.add_reaction('🚂')
        await msg.add_reaction('❌')
        new_train.message_id = msg.id
        await new_train.upsert()
        Train.by_message[msg.id] = new_train
    
    @command()
    async def next(self, ctx):
        train = Train.by_channel.get(ctx.channel.id)
        if not train:
            return
        await train.finish_current_raid()
    
    @command()
    async def join(self, ctx, total: typing.Optional[int]=1, *teamcounts):
        train = Train.by_channel.get(ctx.channel.id)
        if not train:
            return
        meowthuser = MeowthUser.from_id(ctx.bot, ctx.author.id)
        if total or teamcounts:
            party = await meowthuser.party_list(total, *teamcounts)
            await meowthuser.set_party(party=party)
        else:
            party = await meowthuser.party()
        await self._join(meowthuser, train, party=party)
    
    async def _join(self, user, train, party=[0,0,0,1]):
        await user.train_rsvp(train, party=party)
    
    @command()
    async def leave(self, ctx):
        train = Train.by_channel.get(ctx.channel.id)
        if not train:
            return
        meowthuser = MeowthUser.from_id(ctx.bot, ctx.author.id)
        await self._leave(meowthuser, train)
    
    async def _leave(self, user, train):
        await user.leave_train(train)

class TrainEmbed():

    def __init__(self, embed):
        self.embed = embed
    
    title = "Raid Train"
    current_raid_index = 1 
    team_index = 2
    channel_index = 0

    @property
    def team_str(self):
        return self.embed.fields[TrainEmbed.team_index].value
    
    @team_str.setter
    def team_str(self, team_str):
        self.embed.set_field_at(TrainEmbed.team_index, name="Team List", value=team_str)
    
    @classmethod
    async def from_train(cls, train: Train):
        title = cls.title
        current_raid_str = await train.current_raid.train_summary()
        channel_str = train.channel.mention
        team_str = train.team_str
        fields = {
            'Channel': channel_str,
            'Current Raid': current_raid_str,
            'Team List': team_str
        }
        embed = formatters.make_embed(title=title, fields=fields)
        return cls(embed)

class RSVPEmbed():

    def __init__(self, embed):
        self.embed = embed
    
    title = 'Current Train Totals'
    
    @classmethod
    def from_train(cls, train: Train):
        title = cls.title
        team_str = train.team_str
        fields = {
            'Team List': team_str
        }
        embed = formatters.make_embed(title=title, fields=fields)
        return cls(embed)



class RaidEmbed():

    def __init__(self, embed):
        self.embed = embed
    
    @classmethod
    async def from_raid(cls, train: Train, raid: Raid):
        if raid.status == 'active':
            bossfield = "Boss"
            boss = raid.pkmn
            name = await boss.name()
            type_emoji = await boss.type_emoji()
            shiny_available = await boss._shiny_available()
            if shiny_available:
                name += " :sparkles:"
            name += f" {type_emoji}"
            img_url = await boss.sprite_url()
        elif raid.status == 'egg':
            bossfield = "Level"
            name = raid.level
            img_url = raid.bot.raid_info.egg_images[name]
        bot = raid.bot
        end = raid.end
        enddt = datetime.fromtimestamp(end)
        # color = await boss.color()
        gym = raid.gym
        travel_time = "Unknown"
        if isinstance(gym, Gym):
            directions_url = await gym.url()
            directions_text = await gym._name()
            exraid = await gym._exraid()
            if train.current_raid:
                current_gym = train.current_raid.gym
                if isinstance(current_gym, Gym):
                    times = await Mapper.get_travel_times(bot, [current_gym], [gym])
                    travel_time = times[0]['travel_time']
        else:
            directions_url = gym.url
            directions_text = gym._name + " (Unknown Gym)"
            exraid = False
        if exraid:
            directions_text += " (EX Raid Gym)"
        fields = {
            bossfield: name,
            "Gym": f"[{directions_text}]({directions_url})",
            "Travel Time": travel_time
        }
        embed = formatters.make_embed(title="Raid Report", # msg_colour=color,
            thumbnail=img_url, fields=fields, footer="Ending")
        embed.timestamp = enddt
        return cls(embed)




