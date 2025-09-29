using User.API.Models;
using Xunit;

namespace User.API.Tests.Models
{
    public class UserModelTests
    {
        [Fact]
        public void CanSetAndGetProperties()
        {
            var user = new UserModel { Id = 42, Name = "Alice", Email = "alice@email.com" };
            Assert.Equal(42, user.Id);
            Assert.Equal("Alice", user.Name);
            Assert.Equal("alice@email.com", user.Email);
        }
    }
}
