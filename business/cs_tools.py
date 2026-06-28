"""客服业务工具（结构完整版）。

A类只读（is_write=False，agent 自由调）：
  search_products / check_stock / recommend_size / get_order_status
B类写操作（is_write=True，阶段5 需确认）：
  place_order / cancel_order

每个工具的结构：
  Input 定义参数 → Tool 设属性 → __init__ 存 store
  → execute 调 store 方法、处理本层业务情况、返回 ToolResult

异常处理原则：
  - 不重复 Pydantic 已做的参数校验
  - 处理本层真实业务情况（无货/不存在/库存不足/超出尺码范围）
  - 对"可能抛异常的 store 调用"做防御（为以后换数据库准备）
"""

from pydantic import BaseModel, Field
from core.tools import BaseTool, ToolResult, request_token_var
from business.store import Store, UnknownSizeError
from core.memory import upsert_memory, ALLOWED_MEMORY_KEYS


# ============ A类：search_products ============

class SearchProductsInput(BaseModel):
    keyword: str = Field(description="商品关键词，如 air、T恤")


class SearchProductsTool(BaseTool):
    name = "search_products"
    description = (
        "按关键词搜索【有哪些商品】，返回商品列表（名称/价格/品类）。"
        "用于'有没有X''卖什么'这类问题。"
        "查某商品某尺码的具体库存数量，请改用 check_stock。"
    )
    input_model = SearchProductsInput
    is_write = False

    def __init__(self, store: Store):
        self._store = store

    async def execute(self, arguments: SearchProductsInput) -> ToolResult:
        try:
            products = self._store.search_products(arguments.keyword)
        except Exception as e:
            return ToolResult(output=f"搜索失败：{e}", is_error=True)
        if not products:
            return ToolResult(output=f"没有找到包含「{arguments.keyword}」的商品")
        lines = [f"{p['id']} | {p['name']} | ¥{p['price']} | {p['category']}" for p in products]
        return ToolResult(output="找到以下商品：\n" + "\n".join(lines))


# ============ A类：check_stock ============

class CheckStockInput(BaseModel):
    product_id: str = Field(description="商品ID，如 airmax")
    size: str = Field(description="尺码，如 42")


class CheckStockTool(BaseTool):
    name = "check_stock"
    description = (
        "查询【某个具体商品某个尺码的库存数量】。"
        "警告：调用此工具前，必须明确知道用户的具体尺码。如果用户未提供尺码，绝不能自己猜测或默认使用常见尺码，必须先向用户追问。"
        "不确定某商品到底有哪些尺码时，请先用 list_stock 查全部尺码，不要凭常识猜尺码。"
    )
    input_model = CheckStockInput
    is_write = False

    def __init__(self, store: Store):
        self._store = store

    async def execute(self, arguments: CheckStockInput) -> ToolResult:
        try:
            rows = self._store.list_inventory(arguments.product_id)
        except Exception as e:
            return ToolResult(output=f"查询库存失败：{e}", is_error=True)
        if rows is None:
            return ToolResult(output=f"商品 {arguments.product_id} 不存在", is_error=True)
        # 区分"该商品根本没有这个尺码"和"有这个尺码但缺货"——
        # 否则猜出来的幻觉尺码会被当成真实的"无货"尺码（boss 测试里的 bug）。
        match = next((r for r in rows if r["size"] == arguments.size), None)
        if match is None:
            sizes = "、".join(r["size"] for r in rows) or "（暂无任何尺码）"
            return ToolResult(
                output=(
                    f"{arguments.product_id} 没有 {arguments.size} 这个尺码。"
                    f"现有尺码：{sizes}"
                )
            )
        if match["available"] <= 0:
            return ToolResult(output=f"{arguments.product_id} {arguments.size}码 当前无货")
        return ToolResult(
            output=f"{arguments.product_id} {arguments.size}码 有货，库存 {match['available']} 件"
        )


# ============ A类：list_stock ============

class ListStockInput(BaseModel):
    product_id: str = Field(description="商品ID，如 airmax")


class ListStockTool(BaseTool):
    name = "list_stock"
    description = (
        "列出【某个商品的全部真实尺码及各自库存】。"
        "用户问'有哪些尺码''尺码和库存''各尺码还剩多少'这类问题时用它，"
        "一次拿到该商品所有尺码——绝不要自己凭常识罗列 S/M/L/XL 或 28-32 等尺码。"
    )
    input_model = ListStockInput
    is_write = False

    def __init__(self, store: Store):
        self._store = store

    async def execute(self, arguments: ListStockInput) -> ToolResult:
        try:
            rows = self._store.list_inventory(arguments.product_id)
        except Exception as e:
            return ToolResult(output=f"查询库存失败：{e}", is_error=True)
        if rows is None:
            return ToolResult(output=f"商品 {arguments.product_id} 不存在", is_error=True)
        if not rows:
            return ToolResult(output=f"商品 {arguments.product_id} 暂无任何尺码库存")
        lines = [
            f"{r['size']}码：{r['available']} 件" + ("（无货）" if r["available"] <= 0 else "")
            for r in rows
        ]
        return ToolResult(output=f"{arguments.product_id} 各尺码库存：\n" + "\n".join(lines))


# ============ A类：recommend_size ============

class RecommendSizeInput(BaseModel):
    height: int = Field(description="身高，单位cm")
    weight: int = Field(description="体重，单位kg")
    category: str = Field(description="品类，如 鞋 / 上衣")


class RecommendSizeTool(BaseTool):
    name = "recommend_size"
    description = "根据用户身高体重和品类，按尺码表推荐尺码。"
    input_model = RecommendSizeInput
    is_write = False

    def __init__(self, store: Store):
        self._store = store

    async def execute(self, arguments: RecommendSizeInput) -> ToolResult:
        try:
            size = self._store.recommend_size(
                arguments.height, arguments.weight, arguments.category
            )
        except Exception as e:
            return ToolResult(output=f"推荐尺码失败：{e}", is_error=True)
        if size is None:
            # 身高体重不在尺码表范围内（本层业务情况）
            return ToolResult(
                output=f"身高{arguments.height}cm 体重{arguments.weight}kg 暂无匹配的{arguments.category}尺码，建议咨询人工"
            )
        return ToolResult(output=f"建议尺码：{size}")


# ============ A类：get_order_status ============

class GetOrderStatusInput(BaseModel):
    order_id: int = Field(description="订单号")


class GetOrderStatusTool(BaseTool):
    name = "get_order_status"
    description = "根据订单号查询订单状态。"
    input_model = GetOrderStatusInput
    is_write = False

    def __init__(self, store: Store, user_id: str = "default"):
        self._store = store
        self._user_id = user_id

    async def execute(self, arguments: GetOrderStatusInput) -> ToolResult:
        try:
            order = self._store.get_order(arguments.order_id)
        except Exception as e:
            return ToolResult(output=f"查询订单失败：{e}", is_error=True)
        # 归属校验：不是本人的单，与"不存在"合并成同一句回复——
        # 别人能不能凭"不存在/无权查看"两种不同措辞探出某订单号是否存在。
        if order is None or order["user_id"] != self._user_id:
            return ToolResult(output=f"订单 {arguments.order_id} 不存在")
        return ToolResult(
            output=f"订单{order['id']}：{order['product_id']} {order['size']}码 x{order['qty']} 状态:{order['status']}"
        )


# ============ B类：place_order（写操作，阶段5需确认）============

class PlaceOrderInput(BaseModel):
    product_id: str = Field(description="商品ID")
    size: str = Field(description="尺码")
    qty: int = Field(default=1, description="数量")


class PlaceOrderTool(BaseTool):
    name = "place_order"
    description = (
        "发起下单：用户【购买】商品时使用，生成待确认订单并预占库存，不会真正扣款。"
        "返回的订单号需用户在确认接口确认后才真正生效，未确认会自动过期释放。"
        "仅用于用户购买，不要用于商家新增商品或增加库存。"
    )
    input_model = PlaceOrderInput
    is_write = True

    def __init__(self, store: Store, user_id: str = "default"):
        self._store = store
        self._user_id = user_id

    async def execute(self, arguments: PlaceOrderInput) -> ToolResult:
        # agent 只负责"发起"：建草稿 + 预占库存，不执行不可逆的真扣款。
        # 真正生效（扣库存）由独立的后端确认接口完成——模型/工具碰不到那一步。
        # 令牌走工程侧暗线（ContextVar），模型入参里没有它：同一轮重复下单收敛成一张草稿。
        token = request_token_var.get()
        try:
            draft = self._store.create_draft_order(
                arguments.product_id, arguments.size, arguments.qty,
                user_id=self._user_id, request_token=token,
            )
        except ValueError as e:
            # 库存不足（本层业务情况）
            return ToolResult(output=f"下单失败：{e}", is_error=True)
        except Exception as e:
            return ToolResult(output=f"下单失败：{e}", is_error=True)
        return ToolResult(
            output=(
                f"已生成待确认订单 #{draft['id']}："
                f"{arguments.product_id} {arguments.size}码 ×{arguments.qty}，"
                f"请在15分钟内确认。订单号：{draft['id']}"
            ),
            # draft_id 结构化透出：循环层据此把 id 带进事件流，
            # 不依赖解析上面这句人类可读文案（文案一改就会抠错）。
            metadata={"draft_id": draft["id"]},
        )


# ============ B类：cancel_order（写操作，阶段5需确认）============

class CancelOrderInput(BaseModel):
    order_id: int = Field(description="要取消的订单号")


class CancelOrderTool(BaseTool):
    name = "cancel_order"
    description = (
        "取消【已存在的订单】，会改变订单状态并退回库存/释放预占。"
        "用于用户明确要取消某张订单时。下新单请用 place_order。"
    )
    input_model = CancelOrderInput
    is_write = True

    def __init__(self, store: Store, user_id: str = "default"):
        self._store = store
        self._user_id = user_id

    async def execute(self, arguments: CancelOrderInput) -> ToolResult:
        # 先取出订单做归属校验，再决定要不要真取消。
        # store.cancel_order 本身不带归属校验（也给 HTTP / CLI 用），把关在这一层做：
        # 漏了这步，用户就能在对话里凭订单号取消别人的单、释放别人的预占。
        try:
            order = self._store.get_order(arguments.order_id)
        except Exception as e:
            return ToolResult(output=f"取消订单失败：{e}", is_error=True)
        # 不是本人的单，与"不存在"合并成同一句——不暴露订单号是否存在（减少探测面）。
        if order is None or order["user_id"] != self._user_id:
            return ToolResult(output=f"订单 {arguments.order_id} 无法取消（不存在或无权操作）", is_error=True)
        try:
            ok = self._store.cancel_order(arguments.order_id)
        except Exception as e:
            return ToolResult(output=f"取消订单失败：{e}", is_error=True)
        if not ok:
            return ToolResult(output=f"订单 {arguments.order_id} 无法取消（不存在或已取消）", is_error=True)
        return ToolResult(output=f"订单 {arguments.order_id} 已取消")


# ============ B类·商家专属：update_price ============

class UpdatePriceInput(BaseModel):
    product_id: str = Field(description="要改价的商品ID")
    price: int = Field(description="新价格（整数）", gt=0)


class UpdatePriceTool(BaseTool):
    name = "update_price"
    description = (
        "修改【已存在商品】的价格（仅商家）。"
        "用于'把airmax改成500'这类改价。新增一个还不存在的商品请用 add_product。"
    )
    input_model = UpdatePriceInput
    is_write = True
    allowed_roles = {"merchant"}

    def __init__(self, store: Store):
        self._store = store

    async def execute(self, arguments: UpdatePriceInput) -> ToolResult:
        try:
            ok = self._store.set_price(arguments.product_id, arguments.price)
        except Exception as e:
            return ToolResult(output=f"改价失败：{e}", is_error=True)
        if not ok:
            return ToolResult(output=f"商品 {arguments.product_id} 不存在", is_error=True)
        return ToolResult(output=f"已将 {arguments.product_id} 的价格改为 ¥{arguments.price}")


# ============ B类·商家专属：add_product ============

class AddProductInput(BaseModel):
    product_id: str = Field(description="新商品ID（唯一）")
    name: str = Field(description="商品名")
    price: int = Field(description="价格", gt=0)
    category: str = Field(description="品类，如 鞋 / 上衣")


class AddProductTool(BaseTool):
    name = "add_product"
    description = (
        "商家【新增商品品类】时使用（仅商家）。只创建一条新商品记录，"
        "不含库存、不涉及购买。"
        "不要用于用户下单购买（那用 place_order），也不要用于增加现有商品的库存数量。"
    )
    input_model = AddProductInput
    is_write = True
    allowed_roles = {"merchant"}

    def __init__(self, store: Store):
        self._store = store

    async def execute(self, arguments: AddProductInput) -> ToolResult:
        try:
            ok = self._store.add_product(
                arguments.product_id, arguments.name, arguments.price, arguments.category
            )
        except Exception as e:
            return ToolResult(output=f"上架失败：{e}", is_error=True)
        if not ok:
            return ToolResult(output=f"商品 {arguments.product_id} 已存在", is_error=True)
        return ToolResult(output=f"已上架：{arguments.product_id} | {arguments.name} | ¥{arguments.price}")


# ============ B类·商家专属：restock_product ============

class RestockProductInput(BaseModel):
    product_id: str = Field(description="要补货的商品ID")
    size: str = Field(description="尺码，如 42 / L")
    add_qty: int = Field(description="补货数量（在现有库存上【增加】这么多，不是设为这个值）", gt=0)
    confirm_new_size: bool = Field(
        default=False,
        description=(
            "是否允许为该商品【新增一个原本不存在的尺码】。默认 False。"
            "仅当商家已明确确认要新增这个尺码时才设为 True；"
            "不要为了让补货成功而擅自设 True——尺码不对时应先反问商家。"
        ),
    )


class RestockProductTool(BaseTool):
    name = "restock_product"
    description = (
        "给【已存在商品的某个尺码】补货（仅商家）：在现有库存上增加 add_qty 件。"
        "用于'airmax 42码补20件''L码再进10件'这类补货。"
        "注意：这是增量加，不是把库存改成某个绝对值；新增一个还不存在的商品请用 add_product。"
        "若补的尺码该商品并不存在，工具会报错并列出真实尺码——此时不要自行新增，"
        "应先把真实尺码告诉商家并确认，确认后再带 confirm_new_size=true 重试。"
    )
    input_model = RestockProductInput
    is_write = True
    allowed_roles = {"merchant"}

    def __init__(self, store: Store):
        self._store = store

    async def execute(self, arguments: RestockProductInput) -> ToolResult:
        try:
            new_qty = self._store.restock(
                arguments.product_id, arguments.size, arguments.add_qty,
                create_size=arguments.confirm_new_size,
            )
        except UnknownSizeError:
            # 尺码不在该商品下 + 未确认新增：报错并列出真实尺码，逼模型先和商家核实，
            # 不再静默给幻觉尺码建库存行。
            rows = self._store.list_inventory(arguments.product_id) or []
            sizes = "、".join(r["size"] for r in rows) or "（暂无任何尺码）"
            return ToolResult(
                output=(
                    f"商品 {arguments.product_id} 没有 {arguments.size} 这个尺码，未补货。"
                    f"现有尺码：{sizes}。"
                    f"若确实要新增 {arguments.size} 码，请先与商家确认后再带 confirm_new_size=true 重试。"
                ),
                is_error=True,
            )
        except Exception as e:
            return ToolResult(output=f"补货失败：{e}", is_error=True)
        if new_qty is None:
            return ToolResult(
                output=f"商品 {arguments.product_id} 不存在，无法补货（新增商品请用 add_product）",
                is_error=True,
            )
        return ToolResult(
            output=(
                f"已为 {arguments.product_id} {arguments.size}码 补货 {arguments.add_qty} 件，"
                f"当前库存 {new_qty} 件"
            )
        )


# ============ 记忆工具：模型自己决定记什么 ============

class WriteMemoryInput(BaseModel):
    key: str = Field(
        description="记忆类别键。目前只用于尺码：鞋码用 'shoe_size'，上衣码用 'top_size'。"
                    "同一类别再次确认时用同一个 key，会覆盖更新旧值。")
    value: str = Field(
        max_length=64,
        description="该类别的具体值，如鞋码 '42码'、上衣码 'M'。")


class WriteMemoryTool(BaseTool):
    name = "write_memory"
    description = "记住用户的尺码偏好（鞋码/上衣码），跨会话有用。同一类别用同一个 key，已记过就覆盖更新，不要新开 key。"
    input_model = WriteMemoryInput
    is_write = False

    def __init__(self, user_id: str = "default"):
        self._user_id = user_id

    async def execute(self, arguments: WriteMemoryInput) -> ToolResult:
        # 投毒护栏：只允许尺码两类 key 落盘。非白名单（如 role/discount）一律拒绝——
        # 否则会跨会话注入 system_prompt 造成持久污染。prompt 拦不住对抗输入，此处硬拦。
        if arguments.key not in ALLOWED_MEMORY_KEYS:
            return ToolResult(
                output=f"只支持记忆尺码类（{ '、'.join(sorted(ALLOWED_MEMORY_KEYS)) }），不记其它信息。",
                is_error=True,
            )
        upsert_memory(arguments.key, arguments.value, self._user_id)
        return ToolResult(output="已记住")


# ============ 工具表 ============

def build_tools(store: Store, user_id: str = "default") -> dict[str, BaseTool]:
    """建好所有工具，返回工具表（共用同一个 store）。

    商家工具（update_price/add_product）也注册进来——是否可见/可用由 role 在
    run_query 里决定（allowed_roles={"merchant"}），工具本身不知道自己被特殊对待。
    """
    tools = [
        SearchProductsTool(store),
        CheckStockTool(store),
        ListStockTool(store),
        RecommendSizeTool(store),
        GetOrderStatusTool(store, user_id),
        PlaceOrderTool(store, user_id),
        CancelOrderTool(store, user_id),
        WriteMemoryTool(user_id),
        UpdatePriceTool(store),
        AddProductTool(store),
        RestockProductTool(store),
    ]
    return {t.name: t for t in tools}