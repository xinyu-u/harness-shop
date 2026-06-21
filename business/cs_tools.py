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
from core.tools import BaseTool, ToolResult
from business.store import Store
from core.memory import append_memory


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
        "查询【某个具体商品某个尺码的库存数量】，如'airmax 42码还有几件'。"
        "只是想知道有哪些商品、卖什么，请改用 search_products。"
    )
    input_model = CheckStockInput
    is_write = False

    def __init__(self, store: Store):
        self._store = store

    async def execute(self, arguments: CheckStockInput) -> ToolResult:
        try:
            qty = self._store.check_stock(arguments.product_id, arguments.size)
        except Exception as e:
            return ToolResult(output=f"查询库存失败：{e}", is_error=True)
        if qty <= 0:
            return ToolResult(output=f"{arguments.product_id} {arguments.size}码 当前无货")
        return ToolResult(output=f"{arguments.product_id} {arguments.size}码 有货，库存 {qty} 件")


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

    def __init__(self, store: Store):
        self._store = store

    async def execute(self, arguments: GetOrderStatusInput) -> ToolResult:
        try:
            order = self._store.get_order(arguments.order_id)
        except Exception as e:
            return ToolResult(output=f"查询订单失败：{e}", is_error=True)
        if order is None:
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
        try:
            draft = self._store.create_draft_order(
                arguments.product_id, arguments.size, arguments.qty, user_id=self._user_id
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
            )
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

    def __init__(self, store: Store):
        self._store = store

    async def execute(self, arguments: CancelOrderInput) -> ToolResult:
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


# ============ 记忆工具：模型自己决定记什么 ============

class WriteMemoryInput(BaseModel):
    content: str = Field(description="要长期记住的用户信息，如'用户穿42码鞋'")


class WriteMemoryTool(BaseTool):
    name = "write_memory"
    description = "记住关于用户的、跨会话有用的信息（尺码偏好、历史订单等）。只记不能从其他工具重新查到的信息。"
    input_model = WriteMemoryInput
    is_write = False

    def __init__(self, user_id: str = "default"):
        self._user_id = user_id

    async def execute(self, arguments: WriteMemoryInput) -> ToolResult:
        append_memory(arguments.content, self._user_id)
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
        RecommendSizeTool(store),
        GetOrderStatusTool(store),
        PlaceOrderTool(store, user_id),
        CancelOrderTool(store),
        WriteMemoryTool(user_id),
        UpdatePriceTool(store),
        AddProductTool(store),
    ]
    return {t.name: t for t in tools}